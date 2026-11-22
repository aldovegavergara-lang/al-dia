#!/usr/bin/env python3
"""Local Qwen3-TTS (MLX) Spanish renderer for Al día's daily podcast.

Runs in the separate Python environment named by NEWSNEST_PODCAST_PYTHON, so the web app does not
need MLX or the model. It prefers paragraph/whole-sentence chunks, synthesizes each one, trims only
its leading/trailing silence, rejects empty/non-finite/silent samples, concatenates with a short
pause, applies tempo without changing pitch plus loudness normalization, and encodes to MP3. The
duration sidecar is measured on the encoded MP3 itself.

    python scripts/podcast-tts.py --model /models/Qwen3-TTS-... --text-file transcript.txt \
        --output episode.mp3 --voice Vivian --language Spanish

    # Qwen3-TTS Base voice cloning from a local reference pair:
    python scripts/podcast-tts.py ... --ref-audio voice.wav --ref-text-file voice.txt

The caller validates the MP3 header and the JSON sidecar (duration_seconds) before publishing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

DEFAULT_SAMPLE_RATE = 24000
MIN_PEAK = 1e-4
MIN_RMS = 1e-5
PAUSE_SECONDS = 0.28
DEFAULT_TEMPO = 1.07
DEFAULT_SEED = 610
LOUDNORM = "loudnorm=I=-18:TP=-2:LRA=9"

# A whole sentence/paragraph up to this size is kept intact; past it, word chunks are bounded by
# --chunk-chars instead of breaking prose at an arbitrary character count.
MAX_WHOLE_CHARS = 500

# A 512-token budget can truncate a whole 500-character sentence, so every chunk gets an adaptive
# floor; a caller-supplied budget is never allowed to cut it below that or above the hard cap.
TOKENS_PER_WORD = 18
TOKEN_OVERHEAD = 100
MAX_TOKENS_CAP = 1200

# The optional proper-name map is a flat JSON object of bounded literal strings, never regex.
MAX_PRONUNCIATION_TERM_CHARS = 64
MAX_PRONUNCIATION_SPOKEN_CHARS = 256

# Trim only the silence surrounding a complete chunk: RMS over 10 ms windows above 0.005 marks
# speech; 100 ms before and 150 ms after the first/last speech window are retained.
TRIM_WINDOW_SECONDS = 0.010
TRIM_RMS_THRESHOLD = 0.005
TRIM_LEAD_SECONDS = 0.100
TRIM_TAIL_SECONDS = 0.150


def split_text(text: str, limit: int = 350) -> list[str]:
    """Prefer paragraph and whole-sentence boundaries; fall back at ``limit`` characters.

    Whole sentences (or paragraphs) of up to 500 characters are never cut; longer content is
    broken at word boundaries, with a hard character cut only for single words longer than the
    bounded fallback.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    limit = max(80, int(limit))
    whole_limit = max(limit, MAX_WHOLE_CHARS)

    def flush(chunks: list[str], line: str) -> str:
        if line:
            chunks.append(line)
        return ""

    chunks: list[str] = []
    current = ""

    def add_block(block: str) -> None:
        nonlocal current
        block = " ".join(block.split())
        if not block:
            return
        if len(block) > whole_limit:
            current = flush(chunks, current)
            for word in block.split():
                candidate = f"{current} {word}".strip()
                if len(word) > limit:
                    if current:
                        chunks.append(current)
                        current = ""
                    for start_index in range(0, len(word), limit):
                        chunks.append(word[start_index:start_index + limit])
                elif len(candidate) > limit:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            return
        candidate = f"{current} {block}".strip() if current else block
        if len(candidate) > whole_limit and current:
            current = flush(chunks, current)
            current = block
        else:
            current = candidate

    for paragraph in re.split(r"\n\s*\n+", raw):
        for sentence in re.split(r"(?<=[.!?;:])\s+|\n+", paragraph):
            add_block(sentence)
        current = flush(chunks, current)  # a paragraph is its own chunk, even if two short ones would fit
    current = flush(chunks, current)
    return [chunk for chunk in chunks if chunk] or [raw[:limit]]


def adaptive_max_tokens(text: str, requested: int = 0) -> int:
    """Audio-token budget for a whole chunk: ~18 tokens/word + 100, capped at 1200."""
    needed = max(1, len(text.split())) * TOKENS_PER_WORD + TOKEN_OVERHEAD
    if requested > 0:
        needed = max(needed, int(requested))
    return min(MAX_TOKENS_CAP, max(TOKEN_OVERHEAD, needed))


def load_pronunciations(path: str) -> dict[str, str]:
    """Load a flat ``{term: spoken}`` JSON map of bounded literal strings."""
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pronunciations file must contain a JSON object")
    result: dict[str, str] = {}
    for term, spoken in payload.items():
        if not isinstance(term, str) or not isinstance(spoken, str):
            raise ValueError("pronunciations must map strings to strings")
        if not (0 < len(term) <= MAX_PRONUNCIATION_TERM_CHARS):
            raise ValueError("pronunciation terms must be non-empty and bounded")
        if not (0 < len(spoken) <= MAX_PRONUNCIATION_SPOKEN_CHARS):
            raise ValueError("pronunciation replacements must be non-empty and bounded")
        if any(character in term + spoken for character in "\r\n") or term != term.strip() or spoken != spoken.strip():
            raise ValueError("pronunciation entries must be single-line, unpadded strings")
        result[term] = spoken
    return result


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Replace exact terms case-insensitively at word boundaries in TTS input only.

    Every term is escaped before it reaches the regex, so config values are literal text, never
    patterns; display transcripts are left untouched by the caller.
    """
    if not pronunciations or not text:
        return text
    lookup = {term.casefold(): spoken for term, spoken in pronunciations.items()}
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(term) for term in sorted(lookup, key=len, reverse=True)) + r")(?!\w)",
        re.IGNORECASE)
    return pattern.sub(lambda match: lookup[match.group(0).casefold()], text)


def token_cap_reached(result, max_tokens: int) -> bool:
    """Detect a GenerationResult whose token metadata shows generation stopped at the cap."""
    meta = getattr(result, "token_metadata", None) or result
    for flag in ("truncated", "hit_max_tokens", "reached_max_tokens", "stopped_at_token_limit"):
        if bool(getattr(meta, flag, False)):
            return True
    count = getattr(meta, "token_count", None)
    if count is None:
        count = getattr(meta, "generated_tokens", None)
    tokens = getattr(meta, "tokens", None)
    if count is None and tokens is not None:
        try:
            count = len(tokens)
        except TypeError:
            count = None
    return bool(max_tokens > 0 and isinstance(count, (int, float)) and count >= max_tokens)


def validate_samples(samples, label: str = "audio") -> "object":
    """Reject NaN/Inf and silence before any sample reaches the MP3."""
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0:
        raise ValueError(f"{label}: no samples")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{label}: non-finite samples")
    peak = float(np.max(np.abs(array)))
    rms = float(math.sqrt(float(np.mean(array.astype(np.float64) ** 2))))
    if peak < MIN_PEAK or rms < MIN_RMS:
        raise ValueError(f"{label}: silent samples (peak={peak:.6g}, rms={rms:.6g})")
    return array


def trim_silence(samples, sample_rate: int):
    """Trim only leading/trailing silence from one complete generated chunk.

    Speech is a 10 ms RMS window above 0.005. The keep range runs from 100 ms before the first
    speech window to 150 ms after the last one, so attacks and tails survive. Internal gaps and
    quiet (but non-empty) speech are never cut; validation still rejects genuinely silent audio.
    """
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0:
        return array
    window = max(1, int(round(sample_rate * TRIM_WINDOW_SECONDS)))
    speech = []
    for start_index in range(0, array.size, window):
        block = array[start_index:start_index + window].astype(np.float64)
        if math.sqrt(float(np.mean(block * block))) > TRIM_RMS_THRESHOLD:
            speech.append(start_index)
    if not speech:
        return array
    first = max(0, speech[0] - int(sample_rate * TRIM_LEAD_SECONDS))
    last = min(array.size, speech[-1] + window + int(sample_rate * TRIM_TAIL_SECONDS))
    return array[first:last]


def _result_audio(result):
    audio = getattr(result, "audio", result)
    sample_rate = getattr(result, "sample_rate", None)
    return audio, sample_rate


def synthesize(model, text: str, *, voice: str, language: str, instruct: str,
               sample_rate: int, temperature: float, max_tokens: int,
               ref_audio: str | None = None, ref_text: str | None = None,
               truncated: list | None = None) -> tuple[list, int]:
    """Call Qwen3-TTS's generic ``generate`` with the arguments its mode needs.

    Qwen3-TTS Base voice cloning passes ref_audio/ref_text; CustomVoice/VoiceDesign pass
    voice/instruct. Both carry lang_code, max_tokens and temperature -- including temperature 0,
    which must reach the model rather than being dropped as a falsy value. A result whose token
    metadata shows it stopped at the cap is reported on stderr and in ``truncated``.
    """
    if ref_audio is not None:
        kwargs = {"text": text, "ref_audio": ref_audio, "ref_text": ref_text,
                  "lang_code": language, "temperature": temperature}
    else:
        kwargs = {"text": text, "voice": voice, "lang_code": language,
                  "temperature": temperature}
        if instruct:
            kwargs["instruct"] = instruct
    if max_tokens > 0:
        kwargs["max_tokens"] = max_tokens
    output = model.generate(**kwargs)
    if isinstance(output, tuple) and len(output) == 2 and not hasattr(output[0], "audio"):
        waves, rate = output
        sample_rate = int(rate or sample_rate)
        pieces = [validate_samples(wave, "chunk") for wave in waves]
        return pieces, sample_rate
    pieces = []
    for result in output:
        audio, rate = _result_audio(result)
        sample_rate = int(rate or sample_rate)
        pieces.append(validate_samples(audio, "chunk"))
        if token_cap_reached(result, max_tokens):
            if truncated is not None:
                truncated.append(text[:60])
            print(f"warning: generation reached the {max_tokens}-token cap; audio may be truncated",
                  file=sys.stderr)
    if not pieces:
        raise ValueError("model returned no audio")
    return pieces, sample_rate


def write_wav(path: Path, pieces: list, sample_rate: int) -> float:
    import numpy as np

    pause = np.zeros(int(sample_rate * PAUSE_SECONDS), dtype=np.float32)
    joined = []
    for index, piece in enumerate(pieces):
        if index:
            joined.append(pause)
        joined.append(piece.astype(np.float32, copy=False))
    samples = np.concatenate(joined) if joined else np.zeros(0, dtype=np.float32)
    samples = validate_samples(samples, "joined audio")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return len(samples) / float(sample_rate)


def encode_mp3(wav: Path, output: Path, ffmpeg: str, tempo: float = DEFAULT_TEMPO) -> None:
    """Apply atempo (pitch preserving) and loudnorm, then encode the MP3."""
    executable = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
    if not executable:
        raise RuntimeError(f"ffmpeg not found: {ffmpeg}")
    tempo = float(tempo)
    if not 0.5 <= tempo <= 100.0:
        raise ValueError(f"--tempo must be between 0.5 and 100, got {tempo}")
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [executable, "-y", "-loglevel", "error", "-i", str(wav),
         "-af", f"atempo={tempo:g},{LOUDNORM}", "-codec:a", "libmp3lame", "-q:a", "4", str(output)],
        capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError("ffmpeg failed" + (": " + detail[-1] if detail else ""))


def measure_duration(path: Path, ffmpeg: str = "ffmpeg") -> float | None:
    """Report the encoded file's real duration via ffprobe, falling back to a full decode."""
    executable = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
    if not executable or not Path(path).is_file():
        return None
    probe = shutil.which("ffprobe") or executable
    completed = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    if completed.returncode == 0:
        for line in (completed.stdout or "").splitlines():
            if line.strip():
                try:
                    return float(line.strip())
                except ValueError:
                    continue
    with tempfile.TemporaryDirectory(prefix="aldia-tts-") as temp:
        decoded = Path(temp) / "decoded.wav"
        completed = subprocess.run(
            [executable, "-y", "-loglevel", "error", "-i", str(path), str(decoded)],
            capture_output=True, text=True)
        if completed.returncode == 0 and decoded.is_file():
            try:
                with wave.open(str(decoded), "rb") as handle:
                    return handle.getnframes() / float(handle.getframerate())
            except (wave.Error, ZeroDivisionError):
                return None
    return None


def atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--voice", default="Vivian")
    parser.add_argument("--language", default="Spanish")
    parser.add_argument("--instruct", default="")
    parser.add_argument("--chunk-chars", type=int, default=350)
    parser.add_argument("--temperature", type=float, default=0.45)
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--ref-audio", default="")
    parser.add_argument("--ref-text-file", default="")
    parser.add_argument("--pronunciations-file", default="")
    parser.add_argument("--tempo", type=float, default=DEFAULT_TEMPO)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--sidecar", default="")
    args = parser.parse_args()

    output = Path(args.output)
    sidecar = Path(args.sidecar) if args.sidecar else output.with_suffix(".json")
    ref_audio = args.ref_audio or None
    ref_text = Path(args.ref_text_file).read_text(encoding="utf-8").strip() if args.ref_text_file else None
    if bool(ref_audio) != bool(ref_text):
        print("--ref-audio and --ref-text-file must be supplied together", file=sys.stderr)
        return 2
    if not 0.5 <= args.tempo <= 100.0:
        print(f"--tempo must be between 0.5 and 100, got {args.tempo}", file=sys.stderr)
        return 2
    try:
        pronunciations = load_pronunciations(args.pronunciations_file)
    except (OSError, ValueError) as exc:
        print(f"invalid --pronunciations-file: {exc}", file=sys.stderr)
        return 2
    text = Path(args.text_file).read_text(encoding="utf-8")
    chunks = split_text(text, args.chunk_chars)
    if not chunks:
        print("empty transcript", file=sys.stderr)
        return 2
    try:
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model
    except Exception as exc:  # no MLX in the web environment: the deployment points at its own venv
        print(f"mlx-audio is not available in this Python: {exc}", file=sys.stderr)
        return 3
    if os.path.isdir(args.model) and not os.environ.get("HF_HUB_OFFLINE"):
        # A local model path does not need the Hub; telemetry is off too.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        model = load_model(args.model)
        sample_rate = int(getattr(model, "sample_rate", DEFAULT_SAMPLE_RATE) or DEFAULT_SAMPLE_RATE)
        pieces = []
        truncated = []
        for index, chunk in enumerate(chunks, start=1):
            mx.random.seed(args.seed)  # reset per chunk so each one starts from the same state
            print(f"chunk {index}/{len(chunks)} ({len(chunk)} chars)", file=sys.stderr, flush=True)
            spoken = apply_pronunciations(chunk, pronunciations)  # display transcript stays intact
            budget = adaptive_max_tokens(spoken, args.max_tokens)
            rendered, sample_rate = synthesize(model, spoken, voice=args.voice, language=args.language,
                                               instruct=args.instruct, sample_rate=sample_rate,
                                               temperature=args.temperature, max_tokens=budget,
                                               ref_audio=ref_audio, ref_text=ref_text,
                                               truncated=truncated)
            pieces.extend(trim_silence(piece, sample_rate) for piece in rendered)
        if truncated:
            raise RuntimeError(
                f"generation reached the token cap for {len(truncated)} chunk(s); refusing to publish a truncated episode")
        with tempfile.TemporaryDirectory(prefix="aldia-tts-") as temp:
            wav = Path(temp) / "episode.wav"
            write_wav(wav, pieces, sample_rate)
            encode_mp3(wav, output, args.ffmpeg, tempo=args.tempo)
        duration = measure_duration(output, ffmpeg=args.ffmpeg)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("could not measure the encoded MP3 duration")
        atomic_json(sidecar, {
            "duration_seconds": round(duration, 3),
            "sample_rate": sample_rate,
            "chunks": len(chunks),
            "characters": len(text),
            "model": args.model,
            "voice": args.voice,
            "language": args.language,
            "tempo": args.tempo,
            "seed": args.seed,
            "truncated_chunks": len(truncated),
        })
        print(json.dumps({"output": str(output), "duration_seconds": round(duration, 3),
                          "chunks": len(chunks)}), flush=True)
        return 0
    except Exception as exc:
        output.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
