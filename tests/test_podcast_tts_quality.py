"""Synthetic quality tests for scripts/podcast-tts.py: no models, downloads or private files.

Everything MLX-related and every reference voice path is a stub; audio is generated in memory.
The only external tool used is the already-installed ffmpeg/ffprobe for the encoded-duration tests.
"""

import importlib.util
import json
import math
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "podcast-tts.py"


@pytest.fixture(scope="module")
def tts():
    spec = importlib.util.spec_from_file_location("podcast_tts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeModel:
    """Records generate kwargs and returns one synthetic, non-silent result per call."""

    def __init__(self, sample_rate=24000, samples=None, result_attrs=None):
        self.calls = []
        self.sample_rate = sample_rate
        self.samples = np.full(sample_rate // 2, 0.25, dtype=np.float32) if samples is None else samples
        self.result_attrs = result_attrs or {}

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(audio=self.samples.copy(), sample_rate=self.sample_rate,
                                **self.result_attrs)]


def fake_mlx(monkeypatch, model, seeds):
    """Install mlx/mlx_audio stubs so main() never imports or downloads a model."""
    mlx = types.ModuleType("mlx")
    core = types.ModuleType("mlx.core")
    core.random = SimpleNamespace(seed=seeds.append)
    mlx.core = core
    audio = types.ModuleType("mlx_audio")
    tts_pkg = types.ModuleType("mlx_audio.tts")
    utils = types.ModuleType("mlx_audio.tts.utils")
    utils.load_model = lambda name: model
    audio.tts = tts_pkg
    tts_pkg.utils = utils
    for name, module in (("mlx", mlx), ("mlx.core", core), ("mlx_audio", audio),
                         ("mlx_audio.tts", tts_pkg), ("mlx_audio.tts.utils", utils)):
        monkeypatch.setitem(sys.modules, name, module)


def common_args(text_file, output, sidecar):
    return ["podcast-tts.py", "--text-file", str(text_file), "--output", str(output),
            "--model", "/synthetic/model", "--voice", "Vivian", "--language", "Spanish",
            "--chunk-chars", "350", "--sidecar", str(sidecar)]


def test_split_prefers_paragraph_and_whole_sentence_boundaries(tts):
    first = "A" * 240 + ". " + "B" * 240 + "."  # 483 chars: longer than the 350-char fallback
    second = "C" * 200 + "."
    chunks = tts.split_text(first + "\n\n" + second, 350)
    assert chunks == [first, second]
    assert len(chunks[0]) > 350  # whole sentence beats an arbitrary 350-char break
    assert all(len(chunk) <= 500 for chunk in chunks)


def test_split_long_sentence_has_bounded_fallback(tts):
    words = " ".join(["palabra"] * 120)
    chunks = tts.split_text(words, 350)
    assert len(chunks) > 1
    assert all(len(chunk) <= 350 for chunk in chunks)
    assert " ".join(chunks) == words
    assert tts.split_text("x" * 900, 350) == ["x" * 350, "x" * 350, "x" * 200]


def test_base_voice_cloning_routes_to_generate_with_reference(tts):
    model = FakeModel()
    tts.synthesize(model, "Texto de prueba.", voice="Vivian", language="Spanish", instruct="",
                   sample_rate=24000, temperature=0.3, max_tokens=128,
                   ref_audio="/synthetic/voice.wav", ref_text="Voz de referencia sintetica.")
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call == {"text": "Texto de prueba.", "ref_audio": "/synthetic/voice.wav",
                    "ref_text": "Voz de referencia sintetica.", "lang_code": "Spanish",
                    "temperature": 0.3, "max_tokens": 128}
    assert "voice" not in call and "instruct" not in call


@pytest.mark.parametrize("voice,instruct", [("Vivian", ""), ("", "Hablar con calma.")])
def test_custom_voice_and_voice_design_preserve_zero_temperature(tts, voice, instruct):
    model = FakeModel()
    tts.synthesize(model, "Hola.", voice=voice, language="Spanish", instruct=instruct,
                   sample_rate=24000, temperature=0.0, max_tokens=0)
    expected = {"text": "Hola.", "voice": voice, "lang_code": "Spanish", "temperature": 0.0}
    if instruct:
        expected["instruct"] = instruct
    assert model.calls == [expected]
    assert model.calls[0]["temperature"] == 0.0
    assert "max_tokens" not in model.calls[0]


def test_trim_only_strips_outer_silence(tts):
    sample_rate = 1000  # 10 ms == 10 samples, 100 ms == 100 samples, 150 ms == 150 samples
    lead = np.zeros(400, dtype=np.float32)
    attack = np.full(300, 0.5, dtype=np.float32)
    inner_gap = np.zeros(100, dtype=np.float32)
    tail_speech = np.full(300, 0.6, dtype=np.float32)
    tail = np.zeros(400, dtype=np.float32)
    samples = np.concatenate([lead, attack, inner_gap, tail_speech, tail])
    trimmed = tts.trim_silence(samples, sample_rate)
    assert 0 < trimmed.size < samples.size
    np.testing.assert_array_equal(trimmed[:100], np.zeros(100, dtype=np.float32))  # 100 ms retained
    np.testing.assert_array_equal(trimmed[100:400], attack)
    np.testing.assert_array_equal(trimmed[400:500], inner_gap)  # internal gap untouched
    np.testing.assert_array_equal(trimmed[500:800], tail_speech)
    np.testing.assert_array_equal(trimmed[-150:], np.zeros(150, dtype=np.float32))  # 150 ms retained


def test_trim_preserves_quiet_nonempty_speech(tts):
    quiet = np.full(600, 0.001, dtype=np.float32)  # above validation floor, below trim threshold
    trimmed = tts.trim_silence(quiet, 1000)
    np.testing.assert_array_equal(trimmed, quiet)
    tts.validate_samples(trimmed, "quiet")


def test_silence_and_nonfinite_samples_are_rejected(tts):
    silence = np.zeros(1000, dtype=np.float32)
    with pytest.raises(ValueError, match="silent"):
        tts.validate_samples(silence)
    with pytest.raises(ValueError, match="silent"):
        tts.validate_samples(tts.trim_silence(silence, 1000))
    with pytest.raises(ValueError, match="non-finite"):
        tts.validate_samples(np.array([0.1, np.nan, 0.1], dtype=np.float32))


def test_adaptive_token_budget_covers_long_chunks(tts):
    long_sentence = " ".join(["palabra"] * 60)  # ~420 chars: 512 tokens would truncate it
    budget = tts.adaptive_max_tokens(long_sentence, 512)
    assert budget == 60 * 18 + 100
    assert budget > 512
    assert tts.adaptive_max_tokens("palabra " * 200, 0) == 1200  # hard cap
    assert tts.adaptive_max_tokens("hola", 0) == 118


def test_pronunciations_are_exact_case_insensitive_and_literal(tts, tmp_path):
    path = tmp_path / "pron.json"
    path.write_text(json.dumps({"Álvaro": "Albaro", "C++": "ce mas", "Juan Pérez": "Yuan Perez"}),
                    encoding="utf-8")
    mapping = tts.load_pronunciations(str(path))
    spoken = tts.apply_pronunciations("Álvaro y álvaro con C++ y Juan Pérez; Álvaro.", mapping)
    assert spoken == "Albaro y Albaro con ce mas y Yuan Perez; Albaro."
    assert tts.apply_pronunciations("a.b and axb", {r"a.b": "punto"}) == "punto and axb"
    assert tts.apply_pronunciations("sin cambios", {}) == "sin cambios"


def test_pronunciation_map_rejects_invalid_entries(tts, tmp_path):
    cases = [({"a": ["x"]}, "strings"), ({"outer": {"inner": "x"}}, "strings"),
             ({"x" * 65: "y"}, "bounded"), ({"x": "y" * 257}, "bounded"),
             ({"x\ny": "z"}, "single-line")]
    for index, (payload, match) in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            tts.load_pronunciations(str(path))


def test_token_cap_generation_is_surfaced(tts, capsys):
    truncated = []
    model = FakeModel(result_attrs={"token_count": 512, "truncated": True})
    tts.synthesize(model, "texto", voice="Vivian", language="Spanish", instruct="",
                   sample_rate=24000, temperature=0.45, max_tokens=512, truncated=truncated)
    assert len(truncated) == 1
    assert "token cap" in capsys.readouterr().err

    truncated = []
    model = FakeModel(result_attrs={"token_count": 512})
    tts.synthesize(model, "texto", voice="Vivian", language="Spanish", instruct="",
                   sample_rate=24000, temperature=0.45, max_tokens=512, truncated=truncated)
    assert len(truncated) == 1  # token count at the cap implies truncation without a flag

    truncated = []
    model = FakeModel(result_attrs={"token_count": 100})
    tts.synthesize(model, "texto", voice="Vivian", language="Spanish", instruct="",
                   sample_rate=24000, temperature=0.45, max_tokens=512, truncated=truncated)
    assert truncated == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg/ffprobe not installed")
def test_encoded_duration_follows_tempo(tts, tmp_path):
    sample_rate = 24000
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(sample_rate * 2) / sample_rate)).astype(np.float32)
    wav = tmp_path / "episode.wav"
    source_duration = tts.write_wav(wav, [tone], sample_rate)
    assert source_duration == pytest.approx(2.0, abs=0.01)
    mp3 = tmp_path / "episode.mp3"
    tts.encode_mp3(wav, mp3, "ffmpeg", tempo=1.07)
    measured = tts.measure_duration(mp3, "ffmpeg")
    assert measured is not None and math.isfinite(measured)
    assert measured < source_duration - 0.05  # atempo sped the audio up
    assert measured == pytest.approx(source_duration / 1.07, abs=0.12)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_main_seeds_each_chunk_and_records_encoded_duration(tts, tmp_path, monkeypatch):
    seeds = []
    model = FakeModel()
    fake_mlx(monkeypatch, model, seeds)
    text_file = tmp_path / "transcript.txt"
    text_file.write_text("A" * 240 + ". " + "B" * 240 + ".\n\n" + "C" * 200 + ".", encoding="utf-8")
    output = tmp_path / "episode.mp3"
    sidecar = tmp_path / "episode.json"
    monkeypatch.setattr(sys, "argv", common_args(text_file, output, sidecar)
                        + ["--tempo", "1.07", "--seed", "610"])

    assert tts.main() == 0
    assert seeds == [610, 610]  # one reset per chunk
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["chunks"] == 2
    assert payload["tempo"] == 1.07 and payload["seed"] == 610
    assert payload["truncated_chunks"] == 0
    assert payload["duration_seconds"] == pytest.approx(tts.measure_duration(output, "ffmpeg"), abs=0.002)
    assert [call["voice"] for call in model.calls] == ["Vivian", "Vivian"]
    assert all("ref_audio" not in call for call in model.calls)


def test_main_reads_reference_pair_and_pronunciations_for_tts_only(tts, tmp_path, monkeypatch):
    seeds = []
    model = FakeModel()
    fake_mlx(monkeypatch, model, seeds)
    monkeypatch.setattr(tts, "encode_mp3", lambda *args, **kwargs: None)
    monkeypatch.setattr(tts, "measure_duration", lambda *args, **kwargs: 1.0)
    text_file = tmp_path / "transcript.txt"
    text_file.write_text("Álvaro habla.", encoding="utf-8")
    pron_file = tmp_path / "pron.json"
    pron_file.write_text(json.dumps({"Álvaro": "Albaro"}), encoding="utf-8")
    output = tmp_path / "episode.mp3"
    sidecar = tmp_path / "episode.json"
    monkeypatch.setattr(sys, "argv", common_args(text_file, output, sidecar)
                        + ["--ref-audio", "/synthetic/voice.wav", "--ref-text-file",
                           str(tmp_path / "ref.txt"), "--pronunciations-file", str(pron_file)])
    (tmp_path / "ref.txt").write_text("Referencia sintetica.", encoding="utf-8")

    assert tts.main() == 0
    call = model.calls[0]
    assert call["text"] == "Albaro habla."  # only the TTS input was rewritten
    assert call["ref_audio"] == "/synthetic/voice.wav"
    assert call["ref_text"] == "Referencia sintetica."
    assert call["max_tokens"] == tts.adaptive_max_tokens("Albaro habla.", 0)
    assert text_file.read_text(encoding="utf-8") == "Álvaro habla."  # display transcript intact
    assert json.loads(sidecar.read_text(encoding="utf-8"))["characters"] == len("Álvaro habla.")


def test_split_keeps_short_paragraphs_separate(tts):
    """Two short paragraphs must stay in separate chunks even when both fit in one."""
    first = "Noticia uno sobre el ayuntamiento de la ciudad."
    second = "Noticia dos sobre el mercado central."
    assert len(f"{first} {second}") < 500  # a single whole chunk would otherwise hold both
    assert tts.split_text(f"{first}\n\n{second}", 350) == [first, second]
    assert len(tts.split_text(f"{first}\n\n{second}", 350)) == 2


@pytest.mark.parametrize("result_attrs", [
    {"token_count": 512, "truncated": True},  # explicit truncation flag
    {"token_count": 512},                     # generated tokens at the cap, no flag
])
def test_main_refuses_to_publish_truncated_audio(tts, tmp_path, monkeypatch, capsys, result_attrs):
    """A detected cap aborts synthesis before encoding and cleans up any stale artifacts."""
    seeds = []
    model = FakeModel(result_attrs=result_attrs)
    fake_mlx(monkeypatch, model, seeds)
    encoded = []
    monkeypatch.setattr(tts, "encode_mp3", lambda *args, **kwargs: encoded.append(args))
    monkeypatch.setattr(tts, "measure_duration", lambda *args, **kwargs: 1.0)
    text_file = tmp_path / "transcript.txt"
    text_file.write_text("Texto de prueba para sintetizar.", encoding="utf-8")
    output = tmp_path / "episode.mp3"
    sidecar = tmp_path / "episode.json"
    output.write_bytes(b"stale")  # a previous run left artifacts behind
    sidecar.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", common_args(text_file, output, sidecar))

    assert tts.main() == 1
    assert encoded == []  # refused before any ffmpeg encoding
    assert not output.exists() and not sidecar.exists()  # stale artifacts removed on failure
    assert "token cap" in capsys.readouterr().err
