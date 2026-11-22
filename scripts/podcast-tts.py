#!/usr/bin/env python3
"""Local Qwen3-TTS (MLX) Spanish CustomVoice renderer for Al día's daily podcast.

Runs in the separate Python environment named by NEWSNEST_PODCAST_PYTHON, so the web app does not
need MLX or the model. It splits the extractive transcript into sentence-sized chunks, synthesizes
each one, rejects empty/non-finite/silent samples, concatenates with a short pause, and converts the
result to MP3 with ffmpeg.

    python scripts/podcast-tts.py --model /models/Qwen3-TTS-... --text-file transcript.txt \
        --output episode.mp3 --voice Vivian --language Spanish

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


def split_text(text: str, limit: int) -> list[str]:
    """Sentence-aware chunks under ``limit`` characters; long sentences fall back to words."""
    text = " ".join((text or "").split())
    if not text:
        return []
    limit = max(80, int(limit))
    if len(text) <= limit:
        return [text]

    def flush(chunks: list[str], line: str) -> str:
        if line:
            chunks.append(line)
        return ""

    sentences = [part.strip() for part in re.split(r"(?<=[.!?;:])\s+|\n+", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            current = flush(chunks, current)
            for word in sentence.split():
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
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > limit and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    current = flush(chunks, current)
    return [chunk for chunk in chunks if chunk] or [text[:limit]]

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


def _result_audio(result):
    audio = getattr(result, "audio", result)
    sample_rate = getattr(result, "sample_rate", None)
    return audio, sample_rate


def synthesize(model, text: str, *, voice: str, language: str, instruct: str,
               sample_rate: int, temperature: float, max_tokens: int) -> tuple[list, int]:
    """Call the Qwen3-TTS CustomVoice API, tolerating the small shape differences across mlx-audio."""
    kwargs = {"text": text, "speaker": voice, "language": language}
    if instruct:
        kwargs["instruct"] = instruct
    if max_tokens > 0:
        kwargs["max_tokens"] = max_tokens
    if temperature:
        kwargs["temperature"] = temperature
    if hasattr(model, "generate_custom_voice"):
        output = model.generate_custom_voice(**kwargs)
    else:  # a generic mlx-audio model that only exposes generate()
        output = model.generate(text=text, voice=voice, language=language)
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


def encode_mp3(wav: Path, output: Path, ffmpeg: str) -> None:
    executable = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
    if not executable:
        raise RuntimeError(f"ffmpeg not found: {ffmpeg}")
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [executable, "-y", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-q:a", "4", str(output)],
        capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError("ffmpeg failed" + (": " + detail[-1] if detail else ""))


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
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--sidecar", default="")
    args = parser.parse_args()

    output = Path(args.output)
    sidecar = Path(args.sidecar) if args.sidecar else output.with_suffix(".json")
    text = Path(args.text_file).read_text(encoding="utf-8")
    chunks = split_text(text, args.chunk_chars)
    if not chunks:
        print("empty transcript", file=sys.stderr)
        return 2
    try:
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
        for index, chunk in enumerate(chunks, start=1):
            print(f"chunk {index}/{len(chunks)} ({len(chunk)} chars)", file=sys.stderr, flush=True)
            rendered, sample_rate = synthesize(model, chunk, voice=args.voice, language=args.language,
                                               instruct=args.instruct, sample_rate=sample_rate,
                                               temperature=args.temperature, max_tokens=args.max_tokens)
            pieces.extend(rendered)
        with tempfile.TemporaryDirectory(prefix="aldia-tts-") as temp:
            wav = Path(temp) / "episode.wav"
            duration = write_wav(wav, pieces, sample_rate)
            encode_mp3(wav, output, args.ffmpeg)
        atomic_json(sidecar, {
            "duration_seconds": round(duration, 3),
            "sample_rate": sample_rate,
            "chunks": len(chunks),
            "characters": len(text),
            "model": args.model,
            "voice": args.voice,
            "language": args.language,
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
