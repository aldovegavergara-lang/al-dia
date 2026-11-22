"""Synthetic tests for the local Spanish editorial runner: no MLX, model or network is used."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.conftest import ROOT

SCRIPT = ROOT / "scripts" / "podcast-editor.py"


@pytest.fixture
def editor_module():
    spec = importlib.util.spec_from_file_location("podcast_editor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeTokenizer:
    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return "RENDERED-PROMPT"


@pytest.fixture
def fake_mlx(monkeypatch):
    """A minimal mlx_lm stand-in that records calls and replays scripted completions."""
    state = {"tokenizer": FakeTokenizer(), "outputs": [], "generations": [], "loaded": [],
             "samplers": []}
    module = types.ModuleType("mlx_lm")

    def load(path):
        state["loaded"].append(path)
        return object(), state["tokenizer"]

    def generate(model, tokenizer, **kwargs):
        state["generations"].append(kwargs)
        assert kwargs["prompt"] == "RENDERED-PROMPT"
        assert kwargs["verbose"] is False
        assert "temp" not in kwargs and "temperature" not in kwargs
        return state["outputs"].pop(0)

    sample_utils = types.ModuleType("mlx_lm.sample_utils")

    def make_sampler(**kwargs):
        state["samplers"].append(kwargs)
        return {"sampler": kwargs}

    module.load = load
    module.generate = generate
    module.sample_utils = sample_utils
    sample_utils.make_sampler = make_sampler
    monkeypatch.setitem(sys.modules, "mlx_lm", module)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    return state


def _stories(count):
    return [{"id": i, "title": f"Título {i}", "snippet": f"Texto {i}", "publisher": "Medio"}
            for i in range(1, count + 1)]


def _segment(sid, words=30):
    return {"id": sid, "text": " ".join(["noticia"] * (words - 1)) + " noticia."}


def test_read_stories_accepts_only_the_four_expected_fields(editor_module, tmp_path):
    path = tmp_path / "stories.json"
    stories = _stories(2)
    path.write_text(json.dumps(stories, ensure_ascii=False), encoding="utf-8")
    assert editor_module.read_stories(path) == stories
    path.write_text(json.dumps([{**stories[0], "category": "tech"}]), encoding="utf-8")
    with pytest.raises(ValueError):
        editor_module.read_stories(path)


def test_prompt_quotes_untrusted_articles(editor_module):
    batch = _stories(1)
    prompt = editor_module.make_user_prompt(batch)
    assert "no instrucciones" in prompt
    assert json.dumps(batch, ensure_ascii=False) in prompt
    assert "Ids esperados: [1]" in prompt
    assert "Devuelve solo un array JSON" in prompt
    system = editor_module.SYSTEM_PROMPT.lower()
    assert "no confiables" in system
    assert "1 a 3 oraciones" in system  # natural sentences, no word quota
    assert "hash" in system and "configuración" in system  # no hash/config inventories
    assert "papeles" in system  # preserve who did what to whom
    assert '[{"id": 1, "text":' in editor_module.SYSTEM_PROMPT  # JSON skeleton, not fragments


def test_prompt_separates_dated_context_and_bans_invented_billing(editor_module):
    system = editor_module.SYSTEM_PROMPT.lower()
    # Old dated context must never be narrated as a development of today.
    assert "antecedente" in system
    assert "nunca como un desarrollo de hoy" in system
    # Comparing plan consumption units must not fabricate per-token billing.
    assert "precios por token" in system and "facturación" in system


def test_edit_local_batches_one_disables_thinking_and_loads_once(editor_module, fake_mlx):
    stories = _stories(6)
    fake_mlx["outputs"] = [json.dumps([_segment(i)]) for i in range(1, 7)]
    segments = editor_module.edit(stories, "/local/model", provider="local", api_model="gpt-6-luna",
                                  max_tokens=512, temperature=0.2)
    assert [s["id"] for s in segments] == list(range(1, 7))
    assert len(fake_mlx["generations"]) == 6  # weak local model: one story per call
    assert fake_mlx["loaded"] == ["/local/model"]  # loaded only once
    assert fake_mlx["samplers"] == [{"temp": 0.2}]
    assert len(fake_mlx["tokenizer"].calls) == 6


def test_local_edit_repairs_once_and_rejects_a_second_failure(editor_module, fake_mlx):
    stories = _stories(1)
    fake_mlx["outputs"] = [json.dumps([_segment(2)]), json.dumps([_segment(1)])]
    assert editor_module.edit(stories, "/local/model", provider="local", api_model="gpt-6-luna",
                              max_tokens=512, temperature=0.2) == [_segment(1)]
    assert len(fake_mlx["generations"]) == 2
    repair_messages = fake_mlx["tokenizer"].calls[1][0]
    assert [m["role"] for m in repair_messages] == ["system", "user", "assistant", "user"]
    assert "rechazada" in repair_messages[-1]["content"]
    fake_mlx["outputs"] = [json.dumps([_segment(2)]), json.dumps([_segment(3)])]
    with pytest.raises(ValueError):
        editor_module.edit(stories, "/local/model", provider="local", api_model="gpt-6-luna",
                           max_tokens=512, temperature=0.2)


def test_cloud_edit_batches_five_with_a_larger_token_budget(editor_module, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    captured = _fake_openai_response(
        editor_module, monkeypatch,
        {"status": "completed", "output": [{"content": [{"type": "output_text",
          "text": json.dumps([_segment(i) for i in range(1, 6)])}]}]})
    segments = editor_module.edit(
        _stories(5), "", provider="openai", api_model="gpt-6-luna", max_tokens=1400, temperature=0.2)
    assert [s["id"] for s in segments] == list(range(1, 6))
    assert captured["payload"]["max_output_tokens"] == 4096


@pytest.mark.parametrize("output", [
    "",                                                                  # incomplete
    "not json",                                                          # not strict JSON
    json.dumps([_segment(1), _segment(1)]),                              # duplicate
    json.dumps([_segment(1)]),                                           # missing id 2
    json.dumps([_segment(1), _segment(2), _segment(3)]),                 # unknown id 3
    json.dumps([{"id": 1, "text": "   "}, _segment(2)]),                 # empty text
    json.dumps([{"id": 1, "text": "noticia " * 500}, _segment(2)]),      # unbounded
    json.dumps([{"id": 1, "text": "<think>hola</think>"}, _segment(2)]),  # tags
    json.dumps([{"id": 1, "text": "Subió " + "mucho " * 25 + "999 por ciento."}, _segment(2)]),  # invented number
    json.dumps([{"id": "1", "text": "noticia." * 30}, _segment(2)]),     # non-integer id
    json.dumps([{"id": 1, "text": "Noticia " + "con " * 25 + "abc123def4567890 terminó."},
                _segment(2)]),                                          # long hex hash
    json.dumps([{"id": 1, "text": "Noticia " + "con " * 25 + "https://ejemplo.cl terminó."},
                _segment(2)]),                                          # URL
])
def test_validate_batch_rejects_bad_model_output(editor_module, output):
    with pytest.raises(ValueError):
        editor_module.validate_batch(output, _stories(2))


def test_validate_batch_accepts_a_single_fenced_json_object_array(editor_module):
    output = "```json\n" + json.dumps([_segment(1), _segment(2)]) + "\n```"
    assert editor_module.validate_batch(output, _stories(2)) == [
        {"id": 1, "text": _segment(1)["text"]}, {"id": 2, "text": _segment(2)["text"]}]


def test_validation_errors_name_rule_and_id(editor_module):
    """One retry can repair the exact fault: each message names the id and the failed rule."""
    def rejects(text, story, pattern):
        with pytest.raises(ValueError, match=pattern):
            editor_module.validate_batch(json.dumps([{"id": story["id"], "text": text}]), [story])

    rejects(" ".join(["noticia"] * 7), _rich_story(),
            r"segment 1: word count 7 is outside 8-60")
    rejects("El titular no detalla el alcance de la noticia ocurrida ayer en la región sur del país.",
            _rich_story(), r"segment 1: text narrates input-metadata filler")
    tagged = {"id": 2, "title": "T", "snippet": "S", "publisher": "M"}
    rejects("<think>x</think>", tagged, r"segment 2: text contains tags")
    rejects("Noticia con https://ejemplo.cl y más texto aquí para cumplir el mínimo.",
            tagged, r"segment 2: text contains a URL")
    rejects("Noticia con abc123def4567890 y más texto aquí para cumplir el mínimo.",
            tagged, r"segment 2: text contains a long hexadecimal hash")
    numeric = {"id": 1, "title": "P", "snippet": "Anunció 120 millones de inversión.",
               "publisher": "M"}
    rejects("El ministerio anunció una inversión de 20 millones de pesos para las regiones "
            "y las comunas del país durante este año.", numeric,
            r"segment 1: text invents numbers")


def test_main_writes_atomic_strict_json_offline(editor_module, fake_mlx, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # main must force it back on
    model = tmp_path / "model"
    model.mkdir()
    source = tmp_path / "stories.json"
    source.write_text(json.dumps(_stories(2), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "segments.json"
    fake_mlx["outputs"] = [json.dumps([_segment(1)]), json.dumps([_segment(2)])]
    assert editor_module.main(["--model", str(model), "--input", str(source),
                               "--output", str(output)]) == 0
    segments = json.loads(output.read_text(encoding="utf-8"))
    assert [s["id"] for s in segments] == [1, 2]
    assert not list(tmp_path.glob(".*.tmp"))
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_main_refuses_remote_model(editor_module, tmp_path, capsys):
    output = tmp_path / "segments.json"
    assert editor_module.main(["--model", "/not/a/local/dir", "--input", str(tmp_path / "x.json"),
                               "--output", str(output)]) == 2
    assert not output.exists()
    assert "local directory" in capsys.readouterr().err


def test_main_failure_never_writes_partial_output(editor_module, fake_mlx, tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    source = tmp_path / "stories.json"
    source.write_text(json.dumps(_stories(2), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "segments.json"
    fake_mlx["outputs"] = [json.dumps([_segment(1)])]  # missing id 2
    assert editor_module.main(["--model", str(model), "--input", str(source),
                               "--output", str(output)]) == 1
    assert not output.exists()


def test_duplicate_json_keys_are_rejected_not_silently_overwritten(editor_module):
    # The weak local model emitted one object with id/text keys repeated five times; json.loads
    # would keep only the last pair, which must never pass as a complete segment.
    raw = '{"id": 1, "text": "primera", "id": 2, "text": "segunda", "id": 5, "text": "quinta"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        editor_module.decode_output(raw)
    with pytest.raises(ValueError):
        editor_module.validate_batch(raw, _stories(2))


def test_decode_output_unwraps_exactly_one_segments_wrapper(editor_module):
    expected = [{"id": 1, "text": _segment(1)["text"]}, {"id": 2, "text": _segment(2)["text"]}]
    wrapped = json.dumps([{"segments": [_segment(1), _segment(2)]}])
    assert editor_module.validate_batch(wrapped, _stories(2)) == expected
    double = json.dumps([[{"segments": [_segment(1), _segment(2)]}]])
    with pytest.raises(ValueError):
        editor_module.validate_batch(double, _stories(2))


def test_numeral_grounding_is_per_story_not_per_batch(editor_module):
    batch = [
        {"id": 1, "title": "Sin cifras", "snippet": "Texto sin números.", "publisher": "Medio"},
        {"id": 2, "title": "Presupuesto 2026", "snippet": "El ministerio anunció 2026.", "publisher": "Medio"},
    ]
    wrong = "El ministerio " + "anunció " * 22 + "la cifra 2026 para todos."
    with pytest.raises(ValueError):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": wrong}, _segment(2)]), batch)
    right = "La cifra " + "oficial " * 22 + "fue 2026 este año."
    assert editor_module.validate_batch(
        json.dumps([_segment(1), {"id": 2, "text": right}]), batch)[1]["id"] == 2


def test_read_stories_accepts_bounded_context(editor_module, tmp_path):
    story = {**_stories(1)[0], "context": [{
        "title": "Noticia previa", "snippet": "Contexto previo.", "published": "2026-09-01T00:00:00+00:00",
        "url": "https://ejemplo.cl/a", "publisher": "Medio"}]}
    path = tmp_path / "stories.json"
    path.write_text(json.dumps([story], ensure_ascii=False), encoding="utf-8")
    assert editor_module.read_stories(path)[0]["context"] == story["context"]
    story["context"] = story["context"] * 3
    path.write_text(json.dumps([story], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        editor_module.read_stories(path)


def _fake_openai_response(editor_module, monkeypatch, payload):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(editor_module.urllib.request, "urlopen", urlopen)
    return captured


def test_openai_provider_calls_official_api_with_env_key(editor_module, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    body = json.dumps([_segment(1), _segment(2)])
    captured = _fake_openai_response(editor_module, monkeypatch, {
        "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": body}]}],
    })
    source = tmp_path / "stories.json"
    source.write_text(json.dumps(_stories(2), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "segments.json"
    assert editor_module.main(["--model", "", "--input", str(source), "--output", str(output),
                               "--provider", "openai", "--api-model", "gpt-6-luna"]) == 0
    assert [s["id"] for s in json.loads(output.read_text(encoding="utf-8"))] == [1, 2]
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["authorization"] == "Bearer synthetic-key"
    assert captured["payload"]["model"] == "gpt-6-luna"
    assert captured["payload"]["store"] is False
    assert "temperature" not in captured["payload"]
    assert captured["payload"]["max_output_tokens"] == 4096


def test_openai_provider_rejects_incomplete_and_refusal(editor_module, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    for payload in ({"status": "incomplete"}, {"status": "completed", "refusal": "no"}):
        _fake_openai_response(editor_module, monkeypatch, payload)
        with pytest.raises(ValueError):
            editor_module.call_openai("prompt", "gpt-6-luna", 512, 5.0)


def test_openai_http_error_does_not_leak_secret_body(editor_module, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    def fail(_request, timeout):
        raise editor_module.urllib.error.HTTPError(
            "https://api.openai.com/v1/responses", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(editor_module.urllib.request, "urlopen", fail)
    with pytest.raises(ValueError) as exc:
        editor_module.call_openai("prompt", "gpt-6-luna", 512, 5.0)
    assert "401" in str(exc.value) and "synthetic-key" not in str(exc.value)


def test_codex_provider_uses_one_call_strict_schema_and_no_keys(editor_module, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("CODEX_API_KEY", "another-secret")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        schema = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema.read_text(encoding="utf-8"))
        last = Path(command[command.index("--output-last-message") + 1])
        last.write_text(json.dumps({"segments": [_segment(i) for i in range(1, 6)]}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(editor_module.subprocess, "run", fake_run)
    segments = editor_module.edit(
        _stories(5), "", provider="codex", api_model="gpt-6-luna", max_tokens=1400,
        temperature=0.2, codex_binary="codex")
    assert [s["id"] for s in segments] == list(range(1, 6))
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                 "--json", "--output-schema", "--output-last-message"):
        assert flag in command
    assert "--sandbox" in command and command[command.index("--sandbox") + 1] == "read-only"
    assert "shell_tool" in command and "web_search=disabled" in command
    assert command[command.index("--model") + 1] == "gpt-6-luna"
    prompt_text = captured["kwargs"]["input"]
    assert editor_module.SYSTEM_PROMPT in prompt_text
    assert "Ids esperados" in prompt_text
    assert "OBJETO con la clave 'segments'" in prompt_text
    assert "no leas archivos" in prompt_text
    assert captured["kwargs"]["timeout"] == 180.0
    assert Path(captured["kwargs"]["cwd"]) != ROOT  # isolated temp cwd, no project files
    child_env = captured["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in child_env and "CODEX_API_KEY" not in child_env
    assert captured["schema"]["additionalProperties"] is False
    items = captured["schema"]["properties"]["segments"]["items"]
    assert items["additionalProperties"] is False and set(items["required"]) == {"id", "text"}


def test_codex_failure_does_not_fall_back(editor_module, monkeypatch):
    monkeypatch.setattr(editor_module.subprocess, "run",
                        lambda *_a, **_k: subprocess.CompletedProcess([], 2, "", "quota error"))
    with pytest.raises(ValueError):
        editor_module.edit(_stories(1), "", provider="codex", api_model="gpt-6-luna",
                           max_tokens=1400, temperature=0.2)


def test_no_key_in_cli_or_config_and_official_api_only():
    text = Path(SCRIPT).read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in text and "os.environ.get" in text
    assert "--api-key" not in text and '"api_key"' not in text
    assert "https://api.openai.com/v1/responses" in text
    assert "make_sampler" in text and "enable_thinking=False" in text
    assert "hf_hub_offline" in text.lower()


# --- Spoken editorial quality: reject input-metadata filler, keep real uncertainty --------------

_TAIL = "según informaron también las autoridades durante la jornada de ayer en la región sur del país"


def _rich_story():
    return {"id": 1, "title": "Investigación",
            "snippet": "La fiscalía abrió una investigación sobre contratos públicos en la región sur del país.",
            "publisher": "Medio"}


def _narrate(phrase):
    """Extend a phrase with neutral news text so rejection proves the regex, not length."""
    return f"{phrase} {_TAIL}."


@pytest.mark.parametrize("filler", [
    "El titular no detalla el alcance",
    "No se especifica cómo ocurrió",
    "El contexto no precisa cómo funcionará",
    "El titular no incluye comparaciones",
    "No se presentan estimaciones",
    "El artículo no adelanta el desenlace",
])
def test_metadata_filler_is_rejected(editor_module, filler):
    text = _narrate(filler)
    assert len(text.split()) >= 8
    with pytest.raises(ValueError, match="filler"):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": text}]), [_rich_story()])


def test_metadata_filler_is_rejected_even_for_a_short_source(editor_module):
    short = {"id": 1, "title": "Alcance", "snippet": "Alcance", "publisher": "Medio"}
    text = "No se especifica cómo ocurrió el hecho y por eso se informa hoy en la región."
    assert len(text.split()) >= 8
    with pytest.raises(ValueError, match="filler"):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": text}]), [short])


def test_attributed_uncertainty_is_not_filler(editor_module):
    doubt = ("La fiscalía investiga si el dirigente recibió pagos por los contratos públicos y la "
             "causa sigue abierta mientras las autoridades reúnen más antecedentes en la región.")
    assert not editor_module._BOILERPLATE_RE.search(doubt)
    assert len(doubt.split()) >= 20
    assert editor_module.validate_batch(json.dumps([{"id": 1, "text": doubt}]), [_rich_story()]) == [
        {"id": 1, "text": doubt}]

    unknown = ("Por ahora no se sabe quién asumirá el cargo, aunque la empresa confirmó que la "
               "decisión llegará en los próximos días según autoridades de la región.")
    assert not editor_module._BOILERPLATE_RE.search(unknown)
    assert editor_module.validate_batch(json.dumps([{"id": 1, "text": unknown}]), [_rich_story()])

    # "No informó" with a named actor is a reported fact, not commentary on absent input fields.
    not_informed = ("La empresa no informó el alcance de la brecha, pero confirmó que todos los "
                    "clientes afectados fueron contactados durante la jornada por sus equipos.")
    assert not editor_module._BOILERPLATE_RE.search(not_informed)
    assert editor_module.validate_batch(
        json.dumps([{"id": 1, "text": not_informed}]), [_rich_story()])


# --- Conservative cleanup: drop standalone metadata sentences, keep every factual one -----------

_METADATA_SENTENCES = [
    "La información citada no detalla el argumento principal.",
    "La información disponible no precisa la capacidad del equipo.",
    "La información citada no ofrece más detalles sobre el hecho.",
]
_FACT = "La empresa anunció hoy una inversión millonaria en la región sur del país."


def test_standalone_metadata_sentence_is_removed_and_fact_kept(editor_module):
    text = f"{_FACT} {' '.join(_METADATA_SENTENCES)}"
    assert editor_module.validate_batch(
        json.dumps([{"id": 1, "text": text}]), [_rich_story()]) == [{"id": 1, "text": _FACT}]


@pytest.mark.parametrize("filler", _METADATA_SENTENCES)
def test_informacion_citada_disponible_variants_are_removed(editor_module, filler):
    assert editor_module._BOILERPLATE_RE.search(filler)
    assert editor_module.validate_batch(
        json.dumps([{"id": 1, "text": f"{_FACT} {filler}"}]), [_rich_story()]) == [
        {"id": 1, "text": _FACT}]


def test_cleanup_keeps_decimals_and_sentence_order(editor_module):
    fact = "La empresa invirtió 3.5 millones en la región y confirmó el plan para el próximo año."
    story = {**_rich_story(), "snippet": "La empresa invirtió 3.5 millones en la región."}
    text = f"{fact} {_METADATA_SENTENCES[0]}"
    # The decimal "3.5" is not a sentence boundary, so no fact is split or rewritten.
    assert editor_module.validate_batch(
        json.dumps([{"id": 1, "text": text}]), [story]) == [{"id": 1, "text": fact}]


def test_cleanup_preserves_named_actor_uncertainty(editor_module):
    doubt = ("La empresa no informó el alcance de la brecha, pero confirmó que todos los clientes "
             "afectados fueron contactados durante la jornada por sus equipos.")
    assert not editor_module._BOILERPLATE_RE.search(doubt)
    text = f"{doubt} {_METADATA_SENTENCES[0]}"
    assert editor_module.validate_batch(
        json.dumps([{"id": 1, "text": text}]), [_rich_story()]) == [{"id": 1, "text": doubt}]


def test_all_metadata_sentences_fail_with_filler_reason(editor_module):
    text = " ".join(_METADATA_SENTENCES)
    with pytest.raises(ValueError, match="filler"):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": text}]), [_rich_story()])


def test_too_short_after_cleanup_fails_with_wordcount_reason(editor_module):
    short = "La votación se realizará el domingo."
    text = f"{short} {_METADATA_SENTENCES[0]}"
    with pytest.raises(ValueError, match=r"word count \d+ is outside"):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": text}]), [_rich_story()])


@pytest.mark.parametrize("unsafe,reason", [
    ("https://ejemplo.cl", "URL"),
    ("<think>x</think>", "tags"),
    ("abc123def4567890", "hash"),
])
def test_unsafe_content_in_removable_sentence_is_still_rejected(editor_module, unsafe, reason):
    text = f"{_FACT} La información citada no detalla {unsafe} el argumento principal."
    with pytest.raises(ValueError, match=reason):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": text}]), [_rich_story()])


def test_metadata_filler_keeps_exactly_one_repair_retry(editor_module, fake_mlx):
    all_filler = json.dumps([{"id": 1, "text": " ".join(_METADATA_SENTENCES)}])
    repaired = json.dumps([{"id": 1, "text": f"{_FACT} {_METADATA_SENTENCES[0]}"}])
    fake_mlx["outputs"] = [all_filler, repaired]
    assert editor_module.edit(_stories(1), "/local/model", provider="local", api_model="gpt-6-luna",
                              max_tokens=512, temperature=0.2) == [{"id": 1, "text": _FACT}]
    assert len(fake_mlx["generations"]) == 2  # one corrective retry, never more


# --- Universal eight-word floor: a short factual sentence is never padded ----------------------

def test_short_factual_sentence_is_accepted_for_every_source(editor_module):
    short_source = {"id": 1, "title": "Elecciones",
                    "snippet": "Elecciones generales", "publisher": "Medio"}
    short = "La votación se realizará el domingo en todo el país con mesas abiertas desde temprano."
    assert len(short.split()) >= 8
    # The same short, factual sentence is valid whether the input is thin or a full extract.
    for story in (short_source, _rich_story()):
        assert editor_module.validate_batch(json.dumps([{"id": 1, "text": short}]), [story]) == [
            {"id": 1, "text": short}]


def test_eight_word_floor_is_universal(editor_module):
    echo = {"id": 1, "title": "Nueva medida tributaria",
            "snippet": "Nueva medida tributaria", "publisher": "Medio"}
    eight = " ".join(["noticia"] * 8)
    assert editor_module.validate_batch(json.dumps([{"id": 1, "text": eight}]), [echo]) == [
        {"id": 1, "text": eight}]
    assert editor_module.validate_batch(json.dumps([{"id": 1, "text": eight}]), [_rich_story()]) == [
        {"id": 1, "text": eight}]
    with pytest.raises(ValueError):
        editor_module.validate_batch(json.dumps([{"id": 1, "text": " ".join(["noticia"] * 7)}]),
                                     [echo])


# --- Codex: one bounded retry on validation failure, same stories and strict schema -------------

def _fake_codex_outputs(monkeypatch, editor_module, payloads):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["input"])
        last = Path(command[command.index("--output-last-message") + 1])
        last.write_text(payloads[len(calls) - 1], encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(editor_module.subprocess, "run", fake_run)
    return calls


def test_codex_retries_once_with_exact_error_and_same_stories(editor_module, monkeypatch):
    stories = _stories(1)
    bad = json.dumps({"segments": [{"id": 1, "text": "corto"}]})
    good = json.dumps({"segments": [_segment(1)]})
    calls = _fake_codex_outputs(monkeypatch, editor_module, [bad, good])
    assert editor_module.edit(stories, "", provider="codex", api_model="gpt-6-luna",
                              max_tokens=1400, temperature=0.2) == [_segment(1)]
    assert len(calls) == 2
    assert "rechazó tu salida" in calls[1]
    assert "ids [1]" in calls[1]
    assert "MISMAS noticias completas" in calls[1]
    assert editor_module.SYSTEM_PROMPT in calls[1]
    assert json.dumps(stories, ensure_ascii=False) in calls[1]
    assert "'segments'" in calls[1]


def test_codex_retry_is_bounded(editor_module, monkeypatch):
    bad = json.dumps({"segments": [{"id": 1, "text": "corto"}]})
    calls = _fake_codex_outputs(monkeypatch, editor_module, [bad, bad])
    with pytest.raises(ValueError):
        editor_module.edit(_stories(1), "", provider="codex", api_model="gpt-6-luna",
                           max_tokens=1400, temperature=0.2)
    assert len(calls) == 2  # never a third attempt


def test_codex_call_failure_is_not_retried(editor_module, monkeypatch):
    calls = []

    def fail(command, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 2, "", "quota error")

    monkeypatch.setattr(editor_module.subprocess, "run", fail)
    with pytest.raises(ValueError):
        editor_module.edit(_stories(1), "", provider="codex", api_model="gpt-6-luna",
                           max_tokens=1400, temperature=0.2)
    assert len(calls) == 1
