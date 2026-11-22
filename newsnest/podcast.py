"""Daily local podcast: the previous civil day's news, read by a local TTS.

The episode is built from what the reader already stored. When an editor model is configured, a
local Spanish editorial stage rewrites the selected stories into a conversational narration before
TTS; without one, the fallback stays *extractive* (stored headline plus snippet, attributed once).
Neither path speaks an introduction, story numbers or "Cobertura" boilerplate: the cap and the full
source list are visual metadata only. The editorial stage is not fact-checked; it can paraphrase
and err. Read stories are included; hidden and muted items are not.

Runtime files live outside the repository, under ``data_dir()/podcasts``:

    manifest.json                  the latest fully published episode (written last, atomically)
    status.json                    the last generation attempt, success or error
    .generation.lock               one generation at a time, even across processes
    .staging/<uuid>/               private work area, never visible to the app
    episodes/YYYY-MM-DD/episode-<version>.mp3
    episodes/YYYY-MM-DD/episode.json

An episode becomes visible only after the MP3 was validated and moved into its final folder and the
manifest was atomically replaced. A crash leaves the previous manifest in place. Audio is immutable:
each generation writes a fresh UUID-versioned file and the manifest carries the same ``version``, so
a failure during publication never changes the audio an already published episode serves, and no old
version is overwritten. Metadata without a ``version`` is the legacy layout: the first real
episode's ``episode.mp3`` stays unchanged and keeps resolving.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

from newsnest.bootstrap import REPO_ROOT, config_dir, data_dir, env
from newsnest.config import Config, Source, load_config
from newsnest.podcast_context import related_context
from newsnest.db import iso, parse_iso, session, utcnow
from newsnest.digest import make_cards
from newsnest.feed import story_hidden

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Santiago"
DEFAULT_START_HOUR = 3
DEFAULT_READY_HOUR = 4
DEFAULT_MAX_STORIES = 25
DEFAULT_RETRY_MINUTES = 5
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_CHUNK_CHARS = 350
DEFAULT_VOICE = "Vivian"
DEFAULT_LANGUAGE = "Spanish"
DEFAULT_TITLE = "Al día"
DEFAULT_EDITOR_SCRIPT = str(REPO_ROOT / "scripts" / "podcast-editor.py")
DEFAULT_EDITOR_TIMEOUT_SECONDS = 900
DEFAULT_API_MODEL = "gpt-6-luna"
DEFAULT_TEMPO = 1.0
DEFAULT_SEED = 0

# Editorial segments are untrusted model output: no tags/markup, no empty or runaway text.
_NARRATION_TAG_RE = re.compile(
    r"<\s*/?\s*[A-Za-z][^>]*>|<\|[^>]*\||```|\*\*|^\s{0,3}#{1,6}\s|^\s*[-*]\s",
    re.IGNORECASE | re.MULTILINE,
)
_MAX_NARRATION_CHARS = 2000
_MIN_NARRATION_WORDS = 5
_MAX_NARRATION_WORDS = 200

_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre")

_PROCESS_LOCK = threading.Lock()
_TAG_RE = re.compile(r"<[^>]+>")


class PodcastError(RuntimeError):
    """A generation step that must be reported in status.json and retried later."""


@dataclass
class PodcastSettings:
    enabled: bool = False
    config_path: Path | None = None
    timezone: str = DEFAULT_TIMEZONE
    start_hour: int = DEFAULT_START_HOUR
    ready_hour: int = DEFAULT_READY_HOUR
    max_stories: int = DEFAULT_MAX_STORIES
    max_per_source: int = 0
    retry_minutes: int = DEFAULT_RETRY_MINUTES
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    python: str = ""
    model: str = ""
    script: str = str(REPO_ROOT / "scripts" / "podcast-tts.py")
    ffmpeg: str = "ffmpeg"
    voice: str = DEFAULT_VOICE
    language: str = DEFAULT_LANGUAGE
    instruct: str = ""
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    temperature: float = 0.45
    max_tokens: int = 512
    title: str = DEFAULT_TITLE
    # Optional Spanish editorial stage (runs before TTS; off when editor_model is empty).
    editor_python: str = ""
    editor_model: str = ""
    editor_script: str = DEFAULT_EDITOR_SCRIPT
    editor_timeout_seconds: int = DEFAULT_EDITOR_TIMEOUT_SECONDS
    editor_provider: str = "local"  # 'local', 'openai' or 'codex' (all explicit)
    editor_api_model: str = DEFAULT_API_MODEL
    editor_codex_binary: str = "codex"
    # Voice-cloning reference pair, pronunciation map and synthesis controls for the TTS runner.
    ref_audio: str = ""
    ref_text_file: str = ""
    pronunciations_file: str = ""
    tempo: float = DEFAULT_TEMPO
    seed: int = DEFAULT_SEED
    errors: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass
class Story:
    title: str
    snippet: str
    category: str
    published: datetime
    score: float
    publisher: str
    sources: list[dict]
    context: list[dict] = field(default_factory=list)


def podcasts_root() -> Path:
    return data_dir() / "podcasts"


def _pick(*values):
    """First value that was actually set; numeric zero and False are valid settings."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _as_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _as_float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _file_config(path: Path, errors: list[str]) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        errors.append(f"{path.name}: {type(e).__name__}: {e}")
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{path.name}: expected a JSON object")
        return {}
    return raw


def settings() -> PodcastSettings:
    """Environment overrides the optional private JSON file; without either, the podcast is off.

    The file is read only at runtime. Nothing here hardcodes a personal path: deployments set
    NEWSNEST_PODCAST_PYTHON and NEWSNEST_PODCAST_MODEL (or the same keys in the JSON file).
    """
    errors: list[str] = []
    config_path = Path(env("PODCAST_CONFIG", str(data_dir() / "podcast-config.json")) or "")
    raw = _file_config(config_path, errors)
    file_enabled = _as_bool(raw.get("enabled"), True) if config_path.exists() else False
    enabled = _as_bool(env("PODCAST_ENABLED"), file_enabled)
    tz_name = env("PODCAST_TIMEZONE") or raw.get("timezone") or DEFAULT_TIMEZONE
    try:
        ZoneInfo(str(tz_name))
    except Exception:
        errors.append(f"unknown podcast timezone: {tz_name}")
        tz_name = DEFAULT_TIMEZONE
    start_hour = _as_int(_pick(env("PODCAST_START_HOUR"), raw.get("start_hour")),
                         DEFAULT_START_HOUR, 0, 23)
    ready_hour = _as_int(_pick(env("PODCAST_READY_HOUR"), raw.get("ready_hour")),
                         DEFAULT_READY_HOUR, 0, 23)
    max_stories = _as_int(_pick(env("PODCAST_MAX_STORIES"), raw.get("max_stories")),
                          DEFAULT_MAX_STORIES, 0, 200)
    max_per_source = _as_int(_pick(env("PODCAST_MAX_PER_SOURCE"), raw.get("max_per_source")),
                             0, 0, 100)
    retry_minutes = _as_int(_pick(env("PODCAST_RETRY_MINUTES"), raw.get("retry_minutes")),
                            DEFAULT_RETRY_MINUTES, 0, 1440)
    timeout_seconds = _as_int(_pick(env("PODCAST_TIMEOUT_SECONDS"), raw.get("timeout_seconds")),
                              DEFAULT_TIMEOUT_SECONDS, 30, 7200)
    chunk_chars = _as_int(_pick(env("PODCAST_CHUNK_CHARS"), raw.get("chunk_chars")),
                          DEFAULT_CHUNK_CHARS, 80, 2000)
    temperature = _as_float(_pick(env("PODCAST_TEMPERATURE"), raw.get("temperature")), 0.45, 0.0, 1.0)
    max_tokens = _as_int(_pick(env("PODCAST_MAX_TOKENS"), raw.get("max_tokens")), 512, 0, 20000)
    python = str(env("PODCAST_PYTHON") or raw.get("python") or sys.executable)
    model = str(env("PODCAST_MODEL") or raw.get("model") or "")
    script = str(env("PODCAST_SCRIPT") or raw.get("script") or (REPO_ROOT / "scripts" / "podcast-tts.py"))
    ffmpeg = str(env("PODCAST_FFMPEG") or raw.get("ffmpeg") or "ffmpeg")
    voice = str(env("PODCAST_VOICE") or raw.get("voice") or DEFAULT_VOICE)
    language = str(env("PODCAST_LANGUAGE") or raw.get("language") or DEFAULT_LANGUAGE)
    instruct = str(env("PODCAST_INSTRUCT") or raw.get("instruct") or "")
    title = str(env("PODCAST_TITLE") or raw.get("title") or DEFAULT_TITLE)
    editor_python = str(env("PODCAST_EDITOR_PYTHON") or raw.get("editor_python") or python)
    editor_model = str(env("PODCAST_EDITOR_MODEL") or raw.get("editor_model") or "")
    editor_script = str(env("PODCAST_EDITOR_SCRIPT") or raw.get("editor_script") or DEFAULT_EDITOR_SCRIPT)
    editor_timeout = _as_int(_pick(env("PODCAST_EDITOR_TIMEOUT_SECONDS"),
                                   raw.get("editor_timeout_seconds")),
                             DEFAULT_EDITOR_TIMEOUT_SECONDS, 30, 7200)
    provider = str(env("PODCAST_EDITOR_PROVIDER") or raw.get("editor_provider") or "local").lower()
    if provider not in ("local", "openai", "codex"):
        provider = "local"
    api_model = str(env("PODCAST_EDITOR_API_MODEL") or raw.get("editor_api_model") or DEFAULT_API_MODEL)
    codex_binary = str(env("PODCAST_EDITOR_CODEX_BINARY") or raw.get("editor_codex_binary") or "codex")
    ref_audio = str(env("PODCAST_REF_AUDIO") or raw.get("ref_audio") or "")
    ref_text_file = str(env("PODCAST_REF_TEXT_FILE") or raw.get("ref_text_file") or "")
    pronunciations_file = str(env("PODCAST_PRONUNCIATIONS_FILE")
                              or raw.get("pronunciations_file") or "")
    tempo = _as_float(_pick(env("PODCAST_TEMPO"), raw.get("tempo")), DEFAULT_TEMPO, 0.5, 2.0)
    seed = _as_int(_pick(env("PODCAST_SEED"), raw.get("seed")), DEFAULT_SEED, 0, 2_147_483_647)
    return PodcastSettings(
        enabled=enabled, config_path=config_path, timezone=str(tz_name),
        start_hour=start_hour, ready_hour=ready_hour, max_stories=max_stories,
        max_per_source=max_per_source, retry_minutes=retry_minutes,
        timeout_seconds=timeout_seconds, python=python, model=model, script=script,
        ffmpeg=ffmpeg, voice=voice, language=language, instruct=instruct,
        chunk_chars=chunk_chars, temperature=temperature, max_tokens=max_tokens,
        title=title, editor_python=editor_python, editor_model=editor_model,
        editor_script=editor_script, editor_timeout_seconds=editor_timeout,
        editor_provider=provider, editor_api_model=api_model, editor_codex_binary=codex_binary,
        ref_audio=ref_audio, ref_text_file=ref_text_file,
        pronunciations_file=pronunciations_file, tempo=tempo, seed=seed, errors=errors)


# --- calendar ---------------------------------------------------------------

def day_window(day: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """The civil day [00:00, next 00:00) in ``tz``, as UTC-aware datetimes.

    ZoneInfo resolves the two Chilean DST midnights: the first Sunday of September has no 00:00
    (the day starts at 01:00) and the first Sunday of April repeats an hour. The returned bounds
    are absolute instants, so SQL comparisons use the real 23/24/25-hour day.
    """
    tz = tz or ZoneInfo(DEFAULT_TIMEZONE)
    start = datetime.combine(day, time.min, tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_now(now: datetime | None = None, tz: ZoneInfo | None = None) -> datetime:
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz or ZoneInfo(DEFAULT_TIMEZONE))


def target_day(now: datetime | None = None, cfg: PodcastSettings | None = None) -> date | None:
    """The previous civil day once preparation may start; None before the start hour."""
    cfg = cfg or settings()
    local = local_now(now, cfg.tz)
    if local.hour < cfg.start_hour:
        return None
    return local.date() - timedelta(days=1)


def next_start(now: datetime | None = None, cfg: PodcastSettings | None = None) -> datetime:
    cfg = cfg or settings()
    local = local_now(now, cfg.tz)
    start = local.replace(hour=cfg.start_hour, minute=0, second=0, microsecond=0)
    if local >= start:
        start += timedelta(days=1)
    return start.astimezone(timezone.utc)


def spanish_date(day: date) -> str:
    return f"{_DAYS[day.weekday()]} {day.day} de {_MONTHS[day.month - 1]} de {day.year}"


def _coerce_day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("invalid day")
    return date.fromisoformat(text)


# --- selection --------------------------------------------------------------

def _clean(text: str | None, limit: int = 700) -> str:
    """Visible text from a stored headline/snippet; no markup and no invented words."""
    text = unescape(_TAG_RE.sub(" ", text or ""))
    text = " ".join(text.split())
    return text[:limit].rstrip()


def eligible_stories(conn, cfg: Config, sources: list[Source], day: date,
                     settings: PodcastSettings | None = None) -> tuple[list[Story], int]:
    """Every deduplicated story whose article was published inside the civil day.

    Read state is deliberately ignored. Items hidden by the reader, and items the prefilter marked
    muted, are left out. A story is deduplicated by the same story/event grouping as the reader.
    """
    settings = settings or settings_default()
    start, end = day_window(day, settings.tz)
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.published >= ? AND i.published < ? ORDER BY i.id""",
        (iso(start), iso(end))).fetchall()
    names = {r["source_id"]: r["name"] for r in conn.execute("SELECT source_id, name FROM library_sources")}
    names.update({s.id: s.name for s in sources})
    stories: list[Story] = []
    for card in make_cards(rows, cfg, names):
        if story_hidden(card):
            continue
        members = [m for m in [card.lead, *card.others]
                   if m.row["published"] and not m.row["hidden_at"] and m.row["prefilter"] != "muted"]
        if not members:
            continue
        lead = max(members, key=lambda m: (m.final if m.final is not None else -1,
                                           m.row["published"] or ""))
        published = max(parse_iso(m.row["published"]) for m in members)
        source_list, seen = [], set()
        for member in members:
            publisher = _clean(member.row["publisher"] or names.get(member.row["source_id"])
                               or member.row["source_id"], 160)
            key = (member.row["url"], publisher)
            if key in seen:
                continue
            seen.add(key)
            source_list.append({"title": _clean(member.row["title"], 300),
                                "url": member.row["url"], "publisher": publisher})
        # The broadcast extract is the lead's own: a secondary outlet's text is not borrowed under
        # the lead's name, so an extract-less lead announces only its headline.
        snippet = _clean(lead.row["snippet"])
        stories.append(Story(
            title=_clean(lead.row["title"], 300), snippet=snippet,
            category=lead.row["category"] or "news", published=published,
            score=max((m.final for m in members if m.final is not None), default=-1.0),
            publisher=_clean(lead.row["publisher"] or names.get(lead.row["source_id"])
                             or lead.row["source_id"], 160),
            sources=source_list))
    stories.sort(key=lambda s: (s.score, s.published, s.title), reverse=True)
    return stories, len(stories)


def settings_default() -> PodcastSettings:
    return PodcastSettings()


def select_stories(stories: list[Story], max_stories: int, max_per_source: int = 0) -> list[Story]:
    """Round-robin over sections, then publishers: a practical digest instead of one topic's flood.

    ``max_stories=0`` means all eligible stories. ``max_per_source`` is optional (0 = no cap). If a
    cap leaves fewer than ``max_stories``, the transcript says how many were included; it never
    claims to have read all.
    """
    if max_stories == 0:
        return list(stories)
    if max_stories < 0:
        return []
    ordered = sorted(stories, key=lambda s: (s.score, s.published, s.title), reverse=True)
    by_category: "OrderedDict[str, OrderedDict[str, list[Story]]]" = OrderedDict()
    for story in ordered:
        by_category.setdefault(story.category, OrderedDict()).setdefault(story.publisher, []).append(story)
    cursors = {category: 0 for category in by_category}
    selected: list[Story] = []
    used: set[int] = set()
    per_source: Counter = Counter()
    while len(selected) < max_stories:
        progress = False
        for category, publishers in by_category.items():
            names = list(publishers)
            for offset in range(len(names)):
                index = (cursors[category] + offset) % len(names)
                publisher = names[index]
                if max_per_source and per_source[publisher] >= max_per_source:
                    continue
                bucket = publishers[publisher]
                while bucket and id(bucket[0]) in used:
                    bucket.pop(0)
                if not bucket:
                    continue
                story = bucket.pop(0)
                used.add(id(story))
                per_source[publisher] += 1
                selected.append(story)
                cursors[category] = index + 1
                progress = True
                break
            if len(selected) >= max_stories:
                break
        if not progress:
            break
    return selected


# --- narration order ---------------------------------------------------------

def _narration_group(story: Story) -> int:
    """Stable thematic cluster: frontier tech first, then providers, world/news, other tech."""
    text = f"{getattr(story, 'category', '')} {getattr(story, 'title', '')}".lower()
    if (getattr(story, "category", "") == "ai"
            or any(key in text for key in ("harness", "china", "chino",
                                           "inteligencia artificial", "chatbot"))
            or re.search(r"\b(?:ia|llm|gpt|claude|gemini|deepseek)\b", text)):
        return 0
    if any(key in text for key in ("proveedor", "provider", "cloud", "api ")):
        return 1
    if getattr(story, "category", "") in ("world", "news", "international"):
        return 2
    return 3


def group_for_narration(selected: list[Story]) -> list[Story]:
    """Reorder the same selected stories into contiguous themes, keeping score order inside each.

    Selection (and its diversity cap) is untouched: the returned list has exactly the same items.
    Round-robin selection alternates topics for fairness, which sounds choppy when read aloud, so
    narration groups related themes and lets the editor write a few natural transitions instead.
    """
    # A stable sort keeps the caller's order inside a group when scores tie.
    return sorted(selected, key=lambda story: (
        _narration_group(story), -float(getattr(story, "score", -1.0) or -1.0)))


# --- transcript -------------------------------------------------------------

def build_transcript(day: date, selected: list[Story], total_items: int,
                     settings: PodcastSettings | None = None) -> str:
    """Extractive fallback: stored headline and snippet, attributed once, nothing invented.

    No spoken introduction, story numbering, selection/missing explanations, "Cobertura" or outro:
    the cap and the full source list are visual metadata, not narration. ``day``/``total_items``
    stay in the signature so callers can switch between narration modes without changing shape.
    """
    lines = []
    for story in selected:
        headline = story.title.rstrip(".")
        if story.snippet:
            lines.append(f"{headline}. {story.publisher}: {story.snippet.rstrip('.')}.")
        else:
            lines.append(f"{headline}.")
    return "\n".join(lines)


# --- editorial stage ---------------------------------------------------------

def _read_json_any(path: Path):
    """Read a JSON value of any top-level type, or None if absent/invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def editor_story_payload(selected: list[Story]) -> list[dict]:
    """A stable numeric id plus title/snippet/publisher and optional bounded historical context."""
    payload = []
    for number, story in enumerate(selected, start=1):
        item = {"id": number, "title": story.title, "snippet": story.snippet,
                "publisher": story.publisher}
        context = getattr(story, "context", None)
        if context:
            item["context"] = context
        payload.append(item)
    return payload


def _editor_text_ok(text) -> bool:
    """Strict, bounded Spanish narration: no tags, markup or tool/think artifacts."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text or len(text) > _MAX_NARRATION_CHARS or _NARRATION_TAG_RE.search(text):
        return False
    return _MIN_NARRATION_WORDS <= len(text.split()) <= _MAX_NARRATION_WORDS


def validate_editor_segments(raw, expected_ids: list[int]) -> list[str]:
    """Require exactly one clean segment per selected id; never accept partial model output."""
    if not isinstance(raw, list):
        raise PodcastError("editor output is not a JSON array of segments")
    found: dict[int, str] = {}
    for segment in raw:
        if not isinstance(segment, dict):
            raise PodcastError("editor segment is not an object")
        sid = segment.get("id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            raise PodcastError("editor segment has no integer id")
        if sid in found:
            raise PodcastError(f"editor returned duplicate id {sid}")
        if not _editor_text_ok(segment.get("text")):
            raise PodcastError(f"editor segment {sid} is empty, unbounded or has tags/markup")
        found[sid] = segment["text"].strip()
    expected = set(expected_ids)
    unknown = sorted(set(found) - expected)
    missing = sorted(expected - set(found))
    if unknown:
        raise PodcastError(f"editor returned unknown ids: {unknown[:5]}")
    if missing:
        raise PodcastError(f"editor returned no segment for ids: {missing[:5]}")
    return [found[sid] for sid in expected_ids]


def editor_enabled(settings: PodcastSettings) -> bool:
    """Editorial mode is on for an explicit provider, or a configured local model."""
    return settings.editor_provider in ("openai", "codex") or bool(settings.editor_model)


def run_editor(selected: list[Story], staging: Path, settings: PodcastSettings) -> list[str]:
    """Run the Spanish editor and return one narration text per selected story.

    A configured editor that fails, times out or returns anything but strict complete JSON raises
    PodcastError: generation stops and the previously published episode is kept. There is no silent
    fallback to raw/English fragments. The API key (if any) is inherited from the environment and
    never appears in argv, config or logs.
    """
    if not editor_enabled(settings):
        raise PodcastError("no editor provider configured")
    if not settings.editor_python:
        raise PodcastError("NEWSNEST_PODCAST_EDITOR_PYTHON is not configured")
    script = Path(settings.editor_script)
    if not script.is_file():
        raise PodcastError(f"editor script not found: {script}")
    if settings.editor_provider == "local" and not Path(settings.editor_model).is_dir():
        raise PodcastError("local editor model must be a local directory")
    payload = editor_story_payload(selected)
    source = staging / "editor-input.json"
    result = staging / "editor-output.json"
    _atomic_json(source, payload)
    command = [settings.editor_python, str(script), "--model", settings.editor_model,
               "--input", str(source), "--output", str(result),
               "--provider", settings.editor_provider, "--api-model", settings.editor_api_model]
    if settings.editor_provider == "codex":
        command += ["--codex-binary", settings.editor_codex_binary]
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    timeout = min(settings.editor_timeout_seconds, 180) if settings.editor_provider == "codex" \
        else settings.editor_timeout_seconds
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, cwd=str(REPO_ROOT), env=env)
    except subprocess.TimeoutExpired:
        raise PodcastError(f"editor timed out after {timeout}s") from None
    except OSError as e:
        raise PodcastError(f"editor could not start: {e}") from e
    if completed.returncode != 0:
        # Cloud/subscription errors must not leak raw subprocess streams into status or UI.
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        if settings.editor_provider in ("openai", "codex"):
            raise PodcastError(f"editor failed ({settings.editor_provider})")
        raise PodcastError("editor failed" + (": " + detail[-1][:500] if detail else ""))
    raw = _read_json_any(result)
    return validate_editor_segments(raw, [item["id"] for item in payload])


def build_editorial_transcript(segments: list[str]) -> str:
    """Join cleaned segments in story order; the editor supplies the transitions, not the app."""
    return "\n".join(segment.strip() for segment in segments if segment.strip())


# --- storage ----------------------------------------------------------------

def episode_dir(day) -> Path:
    return podcasts_root() / "episodes" / _coerce_day(day).isoformat()


def _contained(root: Path, path: Path) -> bool:
    """True when ``path`` resolves to ``root`` itself or inside it."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return root == resolved or root in resolved.parents


def _parse_version(value) -> str | None:
    """Strict canonical UUID: version strings are ``uuid4()`` values, nothing looser."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _audio_candidate(folder: Path, root: Path, version: str | None) -> Path | None:
    name = "episode.mp3" if version is None else f"episode-{version}.mp3"
    candidate = folder / name
    if not _contained(root, candidate):
        return None
    return candidate if candidate.is_file() else None


def audio_path(day, version=None) -> Path | None:
    """The published MP3 for a strictly validated day and version, or None. Never resolves outside podcasts/.

    ``version=None`` always resolves the legacy ``episode.mp3`` (the first real episode); an
    explicit version resolves the immutable ``episode-<version>.mp3`` of that generation.
    """
    try:
        day = _coerce_day(day)
        folder = episode_dir(day)
    except (ValueError, TypeError):
        return None
    root = podcasts_root().resolve()
    if version is not None:
        version = _parse_version(version)
        if version is None:
            return None
    return _audio_candidate(folder, root, version)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _public_episode(raw: dict) -> dict | None:
    try:
        day = _coerce_day(raw.get("day"))
        # The public episode resolves exactly the version in the metadata; legacy metadata has none.
        audio = audio_path(day, raw.get("version"))
        if audio is None or audio.stat().st_size < 512:
            return None
        duration = float(raw["duration_seconds"])
        if duration <= 0 or duration != duration or duration in (float("inf"), float("-inf")):
            return None
        sources = [s for s in raw.get("sources", [])
                   if isinstance(s, dict) and all(isinstance(s.get(k), str) for k in ("title", "url", "publisher"))]
        version = _parse_version(raw.get("version"))
        mode = raw.get("narration_mode")
        if mode not in ("editorial", "extractive"):
            mode = "extractive"  # backward fallback for legacy metadata
        audio_url = f"/reels/podcast/{day.isoformat()}/audio"
        if version is not None:
            audio_url = f"{audio_url}?v={version}"
        public = {
            "day": day.isoformat(),
            "title": str(raw.get("title") or f"{DEFAULT_TITLE} · {spanish_date(day)}"),
            "duration_seconds": round(duration, 1),
            "item_count": int(raw.get("item_count") or 0),
            "total_items": int(raw.get("total_items") or 0),
            "audio_url": audio_url,
            "transcript": str(raw.get("transcript") or ""),
            "sources": sources,
            "generated_at": str(raw.get("generated_at") or ""),
            "narration_mode": mode,
        }
        if version is not None:
            public["version"] = version
        return public
    except (TypeError, ValueError, OSError):
        return None


def latest_episode() -> dict | None:
    """The latest fully published episode, or None. Reads one atomic manifest and revalidates it."""
    raw = _read_json(podcasts_root() / "manifest.json")
    return _public_episode(raw) if raw else None


def read_status() -> dict:
    raw = _read_json(podcasts_root() / "status.json")
    return raw if raw else {"state": "never", "updated_at": None}


def public_status(now: datetime | None = None) -> dict:
    """Status for management UIs: lifecycle facts only, never subprocess errors or filesystem paths.

    ``is_late`` is true when the podcast is enabled, local time is past ``ready_hour`` and the
    latest published episode is not the previous civil day's; ``ready_at`` is today's ready hour
    (ISO, UTC) and ``expected_day`` the previous civil day.
    """
    status = read_status()
    status.pop("error", None)
    cfg = settings()
    local = local_now(now, cfg.tz)
    status["ready_at"] = iso(datetime.combine(local.date(), time(cfg.ready_hour), cfg.tz))
    expected_day = (local.date() - timedelta(days=1)).isoformat()
    status["expected_day"] = expected_day
    episode = latest_episode()
    status["is_late"] = bool(cfg.enabled and local.hour >= cfg.ready_hour
                             and not (episode and episode.get("day") == expected_day))
    return status


def _write_status(**fields) -> dict:
    current = read_status()
    current.update(fields)
    current["updated_at"] = iso(utcnow())
    _atomic_json(podcasts_root() / "status.json", current)
    return current


@contextmanager
def generation_lock():
    """One generator at a time in this process and across processes; non-blocking for HTTP safety."""
    root = podcasts_root()
    root.mkdir(parents=True, exist_ok=True)
    fd = None
    acquired_process = _PROCESS_LOCK.acquire(blocking=False)
    if not acquired_process:
        yield False
        return
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - macOS/Linux are the supported hosts
            yield True
            return
        fd = os.open(root / ".generation.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            os.close(fd)
        _PROCESS_LOCK.release()


# --- synthesis --------------------------------------------------------------

def _validate_audio(mp3: Path, sidecar: Path) -> float:
    if not mp3.is_file():
        raise PodcastError("TTS did not create the MP3")
    if mp3.stat().st_size < 512:
        raise PodcastError("TTS MP3 is empty or too small")
    with open(mp3, "rb") as fh:
        head = fh.read(4)
    if not (head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)):
        raise PodcastError("TTS output is not an MP3 stream")
    raw = _read_json(sidecar)
    if not raw:
        raise PodcastError("TTS did not write its duration sidecar")
    try:
        duration = float(raw["duration_seconds"])
    except (KeyError, TypeError, ValueError):
        raise PodcastError("TTS sidecar has no valid duration") from None
    if not (duration > 0 and duration < float("inf")):
        raise PodcastError("TTS duration is not a positive finite number")
    return duration


def run_tts(transcript: str, output: Path, settings: PodcastSettings) -> float:
    """Run the MLX TTS in a separate process, with a hard timeout and no shell interpolation."""
    if not settings.model:
        raise PodcastError("NEWSNEST_PODCAST_MODEL is not configured")
    if not settings.python:
        raise PodcastError("NEWSNEST_PODCAST_PYTHON is not configured")
    script = Path(settings.script)
    if not script.is_file():
        raise PodcastError(f"TTS script not found: {script}")
    text_file = output.with_suffix(".txt")
    text_file.write_text(transcript, encoding="utf-8")
    sidecar = output.with_suffix(".json")
    command = [settings.python, str(script), "--text-file", str(text_file), "--output", str(output),
               "--model", settings.model, "--voice", settings.voice, "--language", settings.language,
               "--ffmpeg", settings.ffmpeg, "--chunk-chars", str(settings.chunk_chars),
               "--temperature", str(settings.temperature), "--max-tokens", str(settings.max_tokens),
               "--sidecar", str(sidecar), "--tempo", str(settings.tempo), "--seed", str(settings.seed)]
    if settings.ref_audio and settings.ref_text_file:
        command += ["--ref-audio", settings.ref_audio, "--ref-text-file", settings.ref_text_file]
    if settings.pronunciations_file:
        command += ["--pronunciations-file", settings.pronunciations_file]
    if settings.instruct:
        command += ["--instruct", settings.instruct]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=settings.timeout_seconds, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        raise PodcastError(f"TTS timed out after {settings.timeout_seconds}s") from None
    except OSError as e:
        raise PodcastError(f"TTS could not start: {e}") from e
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise PodcastError("TTS failed" + (": " + detail[-1][:500] if detail else ""))
    return _validate_audio(output, sidecar)


# --- generation -------------------------------------------------------------

def _episode_metadata(day: date, selected: list[Story], total_items: int, transcript: str,
                      duration: float, settings: PodcastSettings, version,
                      narration_mode: str = "extractive",
                      context_sources: list[dict] | None = None) -> dict:
    sources, seen = [], set()
    for story in selected:
        for source in story.sources:
            key = (source["url"], source["publisher"], "story")
            if key not in seen:
                seen.add(key)
                sources.append(source)
    for source in context_sources or []:
        key = (source["url"], source["publisher"], "context")
        if key not in seen:
            seen.add(key)
            sources.append(source)
    return {
        "day": day.isoformat(),
        "title": f"{settings.title} · {spanish_date(day)}",
        "duration_seconds": round(duration, 1),
        "item_count": len(selected),
        "total_items": total_items,
        "version": str(version),
        "audio_url": f"/reels/podcast/{day.isoformat()}/audio?v={version}",
        "transcript": transcript,
        "sources": sources,
        "generated_at": iso(utcnow()),
        "narration_mode": narration_mode,
    }


def generate(day: date, cfg: Config, sources: list[Source], settings: PodcastSettings,
             conn) -> dict:
    """Build, synthesize, validate and atomically publish one day. Raises on any failure."""
    stories, total = eligible_stories(conn, cfg, sources, day, settings)
    if not stories:
        raise PodcastError("no eligible stories published that day")
    selected = group_for_narration(
        select_stories(stories, settings.max_stories, settings.max_per_source))
    context_sources: list[dict] = []
    if editor_enabled(settings):
        # Related past stories come only from the local DB (two per story, 14-day window).
        for story in selected:
            story.context = related_context(conn, story, day, tz=settings.tz)
            for entry in story.context:
                context_sources.append({"title": entry["title"], "url": entry["url"],
                                        "publisher": entry["publisher"],
                                        "published": entry["published"], "context": True})
    root = podcasts_root()
    staging = root / ".staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    try:
        if editor_enabled(settings):
            # A configured editor is authoritative: any failure aborts generation, so a previous
            # episode is kept and raw/external fragments are never read as a silent fallback.
            segments = run_editor(selected, staging, settings)
            transcript = build_editorial_transcript(segments)
            narration_mode = "editorial"
        else:
            transcript = build_transcript(day, selected, total, settings)
            narration_mode = "extractive"
        mp3 = staging / "episode.mp3"
        duration = run_tts(transcript, mp3, settings)
        # Immutable audio: a fresh UUID version per generation, so a failed manifest write below
        # cannot change the audio an already published episode serves, and no old version is
        # overwritten. The legacy episode.mp3 of the first real episode is never touched.
        version = uuid.uuid4()
        metadata = _episode_metadata(day, selected, total, transcript, duration, settings, version,
                                     narration_mode, context_sources)
        final_dir = episode_dir(day)
        final_dir.mkdir(parents=True, exist_ok=True)
        audio = final_dir / f"episode-{version}.mp3"
        if audio.exists():
            raise PodcastError(f"audio version already exists: {audio.name}")
        os.replace(mp3, audio)
        _atomic_json(final_dir / "episode.json", metadata)
        _atomic_json(root / "manifest.json", metadata)
        return metadata
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _existing(day: date) -> dict | None:
    raw = _read_json(podcasts_root() / "manifest.json")
    if not raw or raw.get("day") != day.isoformat():
        return None
    return _public_episode(raw)


def _same_day_wait(previous: dict, day: date, now: datetime, settings: PodcastSettings) -> datetime | None:
    if previous.get("day") != day.isoformat():
        return None
    if previous.get("state") not in ("error", "no_items", "generating", "retry_wait"):
        return None
    last = parse_iso(previous.get("last_attempt"))
    if not last:
        return None
    wait_until = last + timedelta(minutes=settings.retry_minutes)
    return wait_until if now < wait_until else None


def tick(now: datetime | None = None, force: bool = False, day: date | None = None) -> dict:
    """Scheduler entry point. Never raises; every outcome is persisted for the status panel."""
    now = now or utcnow()
    try:
        cfg = settings()
    except Exception as e:  # a broken config must not kill the scheduler
        log.exception("podcast settings failed")
        return {"error": f"{type(e).__name__}: {e}"}
    if not cfg.enabled:
        _write_status(state="disabled", day=None, error=None)
        return {"skipped": "disabled"}
    day = target_day(now, cfg) if day is None else _coerce_day(day)
    if day is None:
        start = next_start(now, cfg)
        _write_status(state="waiting", day=(local_now(now, cfg.tz).date() - timedelta(days=1)).isoformat(),
                      next_attempt=iso(start), error=None)
        return {"skipped": "before start hour", "next_attempt": iso(start)}
    if not cfg.model:
        _write_status(state="error", day=day.isoformat(), error="NEWSNEST_PODCAST_MODEL is not configured",
                      next_attempt=iso(now + timedelta(minutes=cfg.retry_minutes)))
        return {"error": "podcast model is not configured"}
    with generation_lock() as acquired:
        if not acquired:
            return {"skipped": "generation already running"}
        existing = _existing(day)
        if existing and not force:
            _write_status(state="ready", day=day.isoformat(), error=None, last_success=existing["generated_at"],
                          item_count=existing["item_count"], total_items=existing["total_items"])
            return {"episode": existing, "skipped": "already published"}
        previous = read_status()
        wait_until = None if force else _same_day_wait(previous, day, now, cfg)
        if wait_until:
            _write_status(state="retry_wait", day=day.isoformat(), next_attempt=iso(wait_until))
            return {"skipped": "retry later", "next_attempt": iso(wait_until)}
        attempts = int(previous.get("attempts") or 0) + 1 if previous.get("day") == day.isoformat() else 1
        _write_status(state="generating", day=day.isoformat(), last_attempt=iso(now), attempts=attempts,
                      error=None, next_attempt=None)
        try:
            loaded = load_config(config_dir())
            if not loaded.config:
                raise PodcastError("config.yaml is invalid: " + "; ".join(loaded.errors))
            with session() as conn:
                metadata = generate(day, loaded.config, loaded.sources, cfg, conn)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            log.warning("podcast generation failed", extra={"day": day.isoformat(), "error": message})
            _write_status(state="error", day=day.isoformat(), error=message[:1000],
                          last_attempt=iso(now), next_attempt=iso(now + timedelta(minutes=cfg.retry_minutes)),
                          attempts=attempts)
            return {"error": message}
        _write_status(state="ready", day=day.isoformat(), error=None, last_success=metadata["generated_at"],
                      item_count=metadata["item_count"], total_items=metadata["total_items"],
                      next_attempt=None, attempts=attempts)
        return {"episode": metadata}


def main(argv: list[str] | None = None) -> int:
    """``python -m newsnest.podcast --date 2026-09-05``: build one day now.

    Today and future dates are refused: an episode is a history, never an invention of a day
    that has not closed yet.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, help="civil day (America/Santiago) to build")
    parser.add_argument("--force", action="store_true", help="ignore an already published episode")
    parser.add_argument("--now", type=lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
                        if datetime.fromisoformat(value).tzinfo is None
                        else datetime.fromisoformat(value), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    st = settings()
    day = args.date or target_day(args.now or utcnow(), st)
    if day is None:
        print("podcast is waiting for the start hour; use --date to build a closed past day", file=sys.stderr)
        return 2
    if day >= local_now(args.now or utcnow(), st.tz).date():
        print(f"refusing podcast date: {day.isoformat()}", file=sys.stderr)
        return 2
    loaded = load_config(config_dir())
    if not loaded.config:
        print("config.yaml is invalid: " + "; ".join(loaded.errors), file=sys.stderr)
        return 2
    with session() as conn:
        with generation_lock() as acquired:
            if not acquired:
                print("another generation is already running", file=sys.stderr)
                return 3
            existing = _existing(day)
            if existing and not args.force:
                print(json.dumps(existing, ensure_ascii=False, indent=2), flush=True)
                return 0
            try:
                metadata = generate(day, loaded.config, loaded.sources, st, conn)
            except Exception as e:
                message = f"{type(e).__name__}: {e}"
                log.warning("podcast manual generation failed", extra={"day": day.isoformat(), "error": message})
                print(message, file=sys.stderr)
                return 1
    _write_status(state="ready", day=day.isoformat(), error=None, last_success=metadata["generated_at"],
                  item_count=metadata["item_count"], total_items=metadata["total_items"], next_attempt=None)
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
