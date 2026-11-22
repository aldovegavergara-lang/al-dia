"""Synthetic tests for the daily local podcast: boundaries, selection, atomicity and the reader."""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from newsnest.bootstrap import data_dir
from newsnest.config import Source
from newsnest.db import connect, iso, migrate, utcnow
from newsnest.grouping import new_story
from newsnest.models import Item
from newsnest.podcast import (PodcastError, PodcastSettings, audio_path, build_editorial_transcript,
                              build_transcript, day_window, editor_story_payload, eligible_stories,
                              episode_dir, generate, group_for_narration, latest_episode,
                              podcasts_root, public_status, read_status, run_editor, run_tts,
                              select_stories, settings, target_day, tick, validate_editor_segments)
from newsnest.pipeline import ingest
from newsnest.watches import Prefilter
from tests.conftest import ROOT

AMERICA_SANTIAGO = "America/Santiago"


@pytest.fixture
def podcast_env(env, monkeypatch):
    for name in [n for n in os.environ if n.startswith("NEWSNEST_PODCAST_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEWSNEST_PODCAST_ENABLED", "1")
    monkeypatch.setenv("NEWSNEST_PODCAST_PYTHON", "fake-tts-python")
    monkeypatch.setenv("NEWSNEST_PODCAST_MODEL", "/synthetic/model")
    monkeypatch.setenv("NEWSNEST_PODCAST_RETRY_MINUTES", "0")
    monkeypatch.setenv("NEWSNEST_PODCAST_START_HOUR", "3")
    monkeypatch.setenv("NEWSNEST_PODCAST_TIMEZONE", AMERICA_SANTIAGO)
    return env


@pytest.fixture
def santiago_settings():
    return PodcastSettings(timezone=AMERICA_SANTIAGO)


def add_story(conn, cfg, *, source_id="medios", source_name="Medios", category="tech",
              title="Titular sintético", url=None, guid=None, published=None, snippet="Fragmento sintético.",
              story_id=None, **flags):
    url = url or f"https://medios.example/{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    item = ingest(conn, Source(id=source_id, type="rss", category=category, name=source_name, url=url),
                  [Item(source_id, category, title, url, guid or url, published)], Prefilter(cfg), utcnow())[0]
    if story_id is not None:
        conn.execute("UPDATE items SET story_id=? WHERE id=?", (story_id, item))
    if flags:
        sets, values = [], []
        for key, value in flags.items():
            sets.append(f"{key}=?")
            values.append(iso(value) if isinstance(value, datetime) else value)
        conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*values, item))
        if "hidden_at" in flags:
            conn.execute("UPDATE stories SET source_count=1 WHERE id=(SELECT story_id FROM items WHERE id=?)", (item,))
    return item


@pytest.fixture
def fake_tts(monkeypatch, tmp_path):
    calls = []

    def run(transcript, output, st):
        calls.append({"transcript": transcript, "output": output, "settings": st})
        output.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
        output.with_suffix(".json").write_text(json.dumps(
            {"duration_seconds": 12.5, "sample_rate": 24000, "chunks": 1}), encoding="utf-8")
        return 12.5

    monkeypatch.setattr("newsnest.podcast.run_tts", run)
    return calls


# --- calendar and selection -------------------------------------------------

def test_day_window_uses_santiago_and_survives_dst():
    world = ZoneInfo(AMERICA_SANTIAGO)
    normal = day_window(date(2026, 11, 21), world)
    assert normal[1] - normal[0] == timedelta(hours=24)
    # First Sunday of April, 2026 (April 5): the repeated hour makes a 25-hour day.
    morning = datetime(2026, 4, 4, 3, 30, tzinfo=timezone.utc)
    afternoon = datetime(2026, 4, 5, 4, 0, tzinfo=timezone.utc)
    start, end = day_window(date(2026, 4, 4), world)
    assert start <= morning < afternoon <= end
    assert end - start == timedelta(hours=25)
    # First Sunday of September, 2026 (September 6): there is no 00:00 wall clock, so the
    # day starts at 01:00 and lasts 23 hours.
    start, end = day_window(date(2026, 9, 6), world)
    assert start == datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
    assert end - start == timedelta(hours=23)
    inside = datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)
    assert start <= inside < end


def test_target_day_starts_previous_day_when_preparation_may_begin():
    st = PodcastSettings(timezone=AMERICA_SANTIAGO, start_hour=3)
    # At 03:00Z, Santiago is 01:00 on DST day 2026-09-06: the previous civil day is not closed yet.
    assert target_day(datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc), st) is None
    # At 06:00Z, Santiago is 03:00: preparation starts and the previous civil day is the target.
    assert target_day(datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc), st) == date(2026, 9, 5)
    assert target_day(datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc), st) == date(2026, 9, 5)


def test_publication_dates_not_discovery_dates_choose_the_day(env, cfg, conn, santiago_settings):
    now = utcnow()
    published_yesterday = datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc)
    published_today_local = datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc)  # 01:30 Santiago
    day = target_day(datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc), santiago_settings)
    assert day == date(2026, 9, 5)
    older = add_story(conn, cfg, title="Otra sintética sí", published=published_today_local)
    keeps = add_story(conn, cfg, title="Cronología sintética", published=published_yesterday)
    # Discovery/fetch time says today, but publication says yesterday.
    conn.execute("UPDATE items SET fetched_at=? WHERE id=?", (iso(now), older))
    conn.execute("UPDATE items SET fetched_at=? WHERE id=?", (iso(now), keeps))
    stories, total = eligible_stories(conn, cfg, [Source(id="medios", type="rss", category="tech",
                                                         name="Medios", url="https://medios.example/feed")],
                                      day, santiago_settings)
    titles = {s.title for s in stories}
    assert "Cronología sintética" in titles
    assert "Otra sintética sí" not in titles
    assert total == len(stories) == 1


def test_read_and_hidden_and_muted_stories(conn, cfg, santiago_settings):
    hidden_day, muted_day, read_day = date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)
    read_stamp = iso(datetime(2026, 9, 8, tzinfo=timezone.utc))
    add_story(conn, cfg, title="Artículo escondido", published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc),
                             hidden_at=datetime(2026, 9, 5, 23, 1, tzinfo=timezone.utc))
    add_story(conn, cfg, title="Artículo silenciado", published=datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc),
                            prefilter="muted", score_status="muted")
    read_story = add_story(conn, cfg, title="Artículo leído", published=datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc),
                           read_at=read_stamp)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed")]
    for day, expected in ((hidden_day, []), (muted_day, []), (read_day, ["Artículo leído"])):
        stories, _ = eligible_stories(conn, cfg, sources, day, santiago_settings)
        assert {s.title for s in stories} == set(expected)
    conn.execute("UPDATE items SET read_at=NULL WHERE id=?", (read_story,))


def test_stories_are_deduplicated_and_source_list_is_auditable(conn, cfg, santiago_settings):
    published = datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc)
    story_id = new_story(conn, "tech", utcnow())
    conn.execute("UPDATE stories SET source_count=2 WHERE id=?", (story_id,))
    first = add_story(conn, cfg, title="Historia sin sustento", published=published,
                      url="https://medios.example/a", story_id=story_id)
    second = add_story(conn, cfg, title="Historia sin sustento", published=published,
                       url="https://medios.example/b", story_id=story_id)
    assert first != second
    sources = [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed")]
    stories, total = eligible_stories(conn, cfg, sources, date(2026, 9, 5), santiago_settings)
    assert total == len(stories) == 1
    assert [s["url"] for s in stories[0].sources] == ["https://medios.example/a", "https://medios.example/b"]
    assert all({"title", "url", "publisher"} == set(s) for s in stories[0].sources)


def test_snippet_is_attributed_to_the_lead_only(conn, cfg, santiago_settings):
    """The broadcast extract is the lead's: a secondary outlet's text is not borrowed under it."""
    published = datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc)
    story_id = new_story(conn, "tech", utcnow())
    conn.execute("UPDATE stories SET source_count=2 WHERE id=?", (story_id,))
    add_story(conn, cfg, title="Historia sin extracto", published=published,
              url="https://medios.example/a", story_id=story_id)
    add_story(conn, cfg, title="Historia sin extracto", published=published,
              url="https://otro.example/b", story_id=story_id,
              source_id="otro", source_name="Otro")
    sources = [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed"),
               Source(id="otro", type="rss", category="tech", name="Otro", url="https://otro.example/feed")]
    stories, _ = eligible_stories(conn, cfg, sources, date(2026, 9, 5), santiago_settings)
    assert len(stories) == 1
    story = stories[0]
    assert len(story.sources) == 2
    lead_url = next(s["url"] for s in story.sources if s["publisher"] == story.publisher)
    other_url = next(s["url"] for s in story.sources if s["url"] != lead_url)
    # The secondary outlet has an extract; the lead has none.
    conn.execute("UPDATE items SET snippet=? WHERE url=?", ("Fragmento prestado.", other_url))
    stories, _ = eligible_stories(conn, cfg, sources, date(2026, 9, 5), santiago_settings)
    story = stories[0]
    assert story.snippet == ""  # the lead has no extract: nothing is borrowed from the secondary
    transcript = build_transcript(date(2026, 9, 5), [story], 1, PodcastSettings())
    # The extractive fallback reads only the lead's own headline; a missing snippet is not
    # announced, and a secondary outlet's extract is never borrowed under the lead.
    assert "Historia sin extracto" in transcript
    assert "sin fragmento" not in transcript
    assert "Fragmento prestado." not in transcript


def test_selection_is_diverse_and_capped_honestly():
    def s(title, category="tech", publisher="Medios A"):
        return SimpleNamespace(title=title, snippet="", category=category, publisher=publisher,
                               published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc), score=-1.0, sources=[])
    stories = [s("A1", "tech", "A"), s("A2", "tech", "A"), s("A3", "tech", "A"),
               s("W1", "world", "W"), s("W2", "world", "W")]
    assert select_stories(stories, 0, 0) == stories
    selected = select_stories(stories, 5, 0)
    assert len(selected) == 5
    assert {story.category for story in selected} == {"tech", "world"}
    capped = select_stories(stories, 4, 0)
    assert len(capped) == 4
    per_source = select_stories(stories, 4, 1)
    assert {story.title for story in per_source} == {"W2", "A3"}
    transcript = build_transcript(date(2026, 9, 5), per_source, 5, PodcastSettings())
    # The cap and source list are visual metadata now: never spoken, never repeated.
    assert "A3" in transcript and "W2" in transcript
    assert "historias elegibles" not in transcript
    assert "Cobertura:" not in transcript
    assert "Historia 1" not in transcript

# --- generation, atomicity and retries -------------------------------------

def test_generate_publishes_atomically_and_is_idempotent(env, cfg, conn, podcast_env, fake_tts):
    day = date(2026, 9, 5)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed")]
    st = settings()
    add_story(conn, cfg, published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    stories, total_items = eligible_stories(conn, cfg, sources, day, st)
    assert total_items and stories
    metadata = generate(day, cfg, sources, st, conn)
    assert metadata["day"] == "2026-09-05"
    assert metadata["title"] == "Al día · sábado 5 de septiembre de 2026"
    assert metadata["item_count"] >= 1
    assert metadata["transcript"].startswith("Titular sintético.")
    assert metadata["narration_mode"] == "extractive"
    assert len(fake_tts) == 1
    assert audio_path(day, metadata["version"]).is_file()
    assert metadata["audio_url"] == f"/reels/podcast/2026-09-05/audio?v={metadata['version']}"
    assert latest_episode()["version"] == metadata["version"]
    assert latest_episode()["day"] == "2026-09-05"
    assert latest_episode()["audio_url"] == metadata["audio_url"]
    tick(now=datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc))
    status = read_status()
    assert status["state"] == "ready" and status["day"] == "2026-09-05"
    # A later tick for the same day does not call the TTS again.
    again = tick(now=datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc))
    assert again["skipped"] == "already published" and len(fake_tts) == 1
    assert latest_episode()["day"] == "2026-09-05"


def test_regeneration_failure_preserves_previous_audio_and_manifest(env, cfg, conn, podcast_env,
                                                                     fake_tts, monkeypatch):
    """A failed manifest write during same-day regeneration keeps the previous episode's metadata
    and audio bytes identical; the next success points at the new version."""
    import newsnest.podcast as podcast_module
    day = date(2026, 9, 5)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed")]
    add_story(conn, cfg, published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    st = settings()
    first = generate(day, cfg, sources, st, conn)
    first_bytes = audio_path(day, first["version"]).read_bytes()
    assert latest_episode()["version"] == first["version"]

    fail = {"on": True}
    real_atomic_json = podcast_module._atomic_json

    def atomic_json(path, payload):
        if fail["on"] and path == podcasts_root() / "manifest.json":
            raise OSError("synthetic manifest write failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(podcast_module, "_atomic_json", atomic_json)
    with pytest.raises(OSError):
        generate(day, cfg, sources, st, conn)
    # The previous metadata and the previous audio bytes still resolve identically.
    assert latest_episode()["version"] == first["version"]
    assert latest_episode()["audio_url"] == f"/reels/podcast/2026-09-05/audio?v={first['version']}"
    assert audio_path(day, first["version"]).read_bytes() == first_bytes

    # New success then points at the new version; the old version is never overwritten.
    fail["on"] = False
    second = generate(day, cfg, sources, st, conn)
    assert second["version"] != first["version"]
    assert latest_episode()["version"] == second["version"]
    assert latest_episode()["audio_url"] == f"/reels/podcast/2026-09-05/audio?v={second['version']}"
    assert audio_path(day, second["version"]).is_file()
    assert audio_path(day, first["version"]).read_bytes() == first_bytes


def test_legacy_episode_without_version_uses_legacy_audio(env):
    """Metadata without a version resolves the legacy episode.mp3 of the first real episode."""
    day = date(2026, 9, 5)
    legacy = episode_dir(day) / "episode.mp3"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
    manifest = {
        "day": day.isoformat(),
        "title": "Al día · sábado 5 de septiembre de 2026",
        "duration_seconds": 12.5,
        "item_count": 1,
        "total_items": 1,
        "audio_url": "/reels/podcast/2026-09-05/audio",
        "transcript": "episodio legacy",
        "sources": [{"title": "Título sintético", "url": "https://medios.example/a", "publisher": "Medios"}],
        "generated_at": "2026-09-05T06:04:12+00:00",
    }
    (podcasts_root() / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    episode = latest_episode()
    assert episode is not None
    assert episode["day"] == day.isoformat()
    assert episode["audio_url"] == "/reels/podcast/2026-09-05/audio"
    assert "version" not in episode
    assert audio_path(day).is_file()
    assert audio_path(day).name == "episode.mp3"


def test_public_status_is_late_after_ready_hour_without_the_previous_day(podcast_env, monkeypatch):
    status = public_status(now=datetime(2026, 11, 22, 7, 0, tzinfo=timezone.utc))
    assert status["is_late"] is True
    assert status["expected_day"] == "2026-11-21"
    assert status["ready_at"].startswith("2026-11-22T07:00:00")  # 04:00 in America/Santiago
    assert public_status(now=datetime(2026, 11, 22, 3, 30, tzinfo=timezone.utc))["is_late"] is False
    monkeypatch.setenv("NEWSNEST_PODCAST_ENABLED", "0")
    assert public_status(now=datetime(2026, 11, 22, 7, 0, tzinfo=timezone.utc))["is_late"] is False


def test_public_status_is_not_late_when_the_previous_day_is_published(env, cfg, conn, podcast_env,
                                                                       fake_tts):
    add_story(conn, cfg, published=datetime(2026, 11, 21, 23, 0, tzinfo=timezone.utc))
    generate(date(2026, 11, 21), cfg,
             [Source(id="medios", type="rss", category="tech", name="Medios", url="https://medios.example/feed")],
             settings(), conn)
    status = public_status(now=datetime(2026, 11, 22, 7, 0, tzinfo=timezone.utc))
    assert status["is_late"] is False
    assert status["expected_day"] == "2026-11-21"


def test_failure_publishes_nothing_and_retries(env, cfg, conn, podcast_env, fake_tts, monkeypatch):
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("NEWSNEST_PODCAST_RETRY_MINUTES", "5")
    add_story(conn, cfg, published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))

    def fail(_text, _output, _st):
        raise PodcastError("synthetic failure")

    monkeypatch.setattr("newsnest.podcast.run_tts", fail)
    result = tick(now=now)
    assert result["error"].endswith("synthetic failure")
    assert latest_episode() is None
    assert read_status()["state"] == "error"
    assert not any((data_dir() / "podcasts" / "episodes").glob("*/*.mp3"))

    def success(_text, output, _st):
        output.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
        output.with_suffix(".json").write_text(json.dumps(
            {"duration_seconds": 12.5, "sample_rate": 24000, "chunks": 1}), encoding="utf-8")
        return 12.5
    monkeypatch.setattr("newsnest.podcast.run_tts", success)
    result = tick(now=now + timedelta(minutes=1))
    assert result["skipped"] == "retry later"
    assert read_status()["state"] in ("error", "retry_wait")

    result = tick(now=now + timedelta(minutes=6))  # one minute past the retry interval
    assert result["episode"]["day"] == "2026-09-05"
    assert latest_episode()["day"] == "2026-09-05"
    assert read_status()["state"] == "ready"


def test_waiting_before_start_and_catch_up_after(env, cfg, conn, podcast_env, fake_tts, monkeypatch):
    monkeypatch.setenv("NEWSNEST_PODCAST_START_HOUR", "3")
    monkeypatch.setenv("NEWSNEST_PODCAST_MAX_STORIES", "3")
    # Before 03:00 the previous day is not scheduled, even after a restart.
    early = tick(now=datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc))
    assert early["skipped"] == "before start hour"
    assert read_status()["state"] == "waiting"
    assert len(fake_tts) == 0
    # At or after 03:00 (or a restart after 04:00), the closed day is caught up.
    add_story(conn, cfg, published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    late = tick(now=datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc), force=True)
    assert late["episode"]["day"] == "2026-09-05"


def test_cli_refuses_future_dates(podcast_env):
    from newsnest.podcast import main
    assert main(["--date", "2027-01-01"]) == 2


def test_cli_refuses_today_and_future_dates(podcast_env):
    """Today is not closed yet, so the CLI refuses it as well as future dates."""
    from newsnest.podcast import local_now, main
    assert main(["--date", local_now().date().isoformat()]) == 2
    assert main(["--date", "2027-01-01"]) == 2


def test_disabled_by_default_and_file_enables(env, monkeypatch):
    for name in [n for n in os.environ if n.startswith("NEWSNEST_PODCAST_")]:
        monkeypatch.delenv(name, raising=False)
    assert settings().enabled is False
    (env.data / "podcast-config.json").write_text(json.dumps({"enabled": True, "model": "/synthetic/model"}),
                                                  encoding="utf-8")
    assert settings().enabled is True
    monkeypatch.setenv("NEWSNEST_PODCAST_ENABLED", "0")
    assert settings().enabled is False


@pytest.fixture
def reader(env, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    monkeypatch.setenv("NEWSNEST_PASSWORD", "synthetic-password")
    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO stories (category, first_seen, last_seen, source_count) VALUES ('tech', ?, ?, 1)",
                 (iso(utcnow()), iso(utcnow())))
    story_id = conn.execute("SELECT id FROM stories ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO items (source_id, category, guid, url, url_norm, title, published, fetched_at, story_id)"
                 " VALUES ('medios', 'tech', 'a', 'https://medios.example/a', 'a', 'Título sintético', ?, ?, ?)",
                 (iso(utcnow()), iso(utcnow()), story_id))
    conn.commit()
    conn.close()
    from newsnest.web import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        yield client


# --- web endpoint ------------------------------------------------------------

@pytest.mark.auth
def test_audio_endpoint_is_authenticated_and_range_capable(env, cfg, conn, podcast_env, fake_tts, reader):
    add_story(conn, cfg, title="Noticia para el podcast", published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc),
              snippet="Snippet del medio.")
    episode = generate(date(2026, 9, 5), cfg,
                       [Source(id="medios", type="rss", category="tech", name="Medios",
                               url="https://medios.example/feed")],
                       settings(), conn)
    unauth = reader.get(episode["audio_url"], follow_redirects=False)
    assert unauth.status_code == 303  # the login redirect, never the file
    reader.post("/login", data={"password": "synthetic-password"}, follow_redirects=False)
    audio = reader.get(episode["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.headers["accept-ranges"] == "bytes"
    assert len(audio.content) > 0
    # New layout serves the versioned URL; without v the legacy layout would resolve episode.mp3.
    assert reader.get("/reels/podcast/2026-09-05/audio").status_code == 404
    assert reader.get("/reels/podcast/2026-09-05/audio?v=not-a-version").status_code == 404
    ranged = reader.get(episode["audio_url"], headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert reader.get("/reels/podcast/not-a-day/audio").status_code == 404
    assert reader.get("/reels/podcast/../../../../etc/passwd/audio").status_code in (400, 404, 422)
    assert audio_path("../2026-09-05") is None
    assert audio_path("2026-13-01") is None


def test_podcast_variable_is_only_on_unfiltered_first_page(reader, env, monkeypatch, fake_tts):
    import newsnest.reels as reels
    calls = []

    def spy():
        calls.append(1)
        return {"day": "2026-09-05", "transcript": "synthetic"}

    monkeypatch.setattr(reels, "latest_episode", spy)
    reader.get("/reels")
    assert calls  # the first unfiltered page asks for it
    calls.clear()
    for query in ("?view=read", "?view=saved", "?f=tech", "?source=medios", "?q=test", "?topic=china",
                  "?period=72", "?reaction=liked", "?exclude=test", "?bookmarked=1", "?after=1",
                  "?hours=72", "?saved=1", "?view=latest&hours=72"):
        reader.get(f"/reels{query}")
    assert not calls


# --- TTS script --------------------------------------------------------------

def _tts_script() -> tuple[object, str]:
    path = ROOT / "scripts" / "podcast-tts.py"
    spec = importlib.util.spec_from_file_location("podcast_tts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, path.read_text(encoding="utf-8")


def test_tts_script_chunking_respects_limit_and_never_drops_text():
    script, _ = _tts_script()
    text = ("Primera frase larga que se desborda si no hay saltos. " * 10 + "\n\n"
            + "Segunda parágrafo. " + "Medio " * 50)
    chunks = script.split_text(text, 120)
    assert chunks
    # A whole sentence up to MAX_WHOLE_CHARS stays intact; only longer blocks fall back to the
    # --chunk-chars word limit. No input word is dropped.
    assert all(len(chunk) <= max(120, script.MAX_WHOLE_CHARS) for chunk in chunks)
    assert " ".join(chunks).split() == text.split()
    assert script.split_text("", 120) == []
    long_word = script.split_text("x" * 900, 120)
    assert all(chunk and len(chunk) <= 120 for chunk in long_word)
    assert "".join(long_word) == "x" * 900


# --- local editorial stage ----------------------------------------------------

@pytest.fixture
def fake_editor(monkeypatch):
    """Replace the editor subprocess with a deterministic writer of clean segments."""
    calls = []

    def run(selected, staging, st):
        calls.append({"selected": selected, "staging": staging, "settings": st})
        payload = editor_story_payload(selected)
        segments = [{"id": p["id"], "text": f"Noticia {p['id']}: {p['title']}."} for p in payload]
        (staging / "editor-output.json").write_text(
            json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        return [segment["text"] for segment in segments]

    monkeypatch.setattr("newsnest.podcast.run_editor", run)
    return calls


def _synthetic_story(title):
    return SimpleNamespace(title=title, snippet="Fragmento sintético.", publisher="Medios")


def test_narration_groups_topics_without_changing_selection_counts():
    def s(title, category, score):
        return SimpleNamespace(title=title, category=category, score=score,
                               published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc),
                               snippet="", publisher="Medios", sources=[])
    selected = [s("IA china", "ai", 1), s("Mundial", "world", 2), s("Harness", "tech", 3),
                s("Proveedor cloud", "tech", 4), s("Otra IA", "ai", 5), s("Elecciones", "world", 6)]
    grouped = group_for_narration(selected)
    assert len(grouped) == len(selected)
    assert {id(story) for story in grouped} == {id(story) for story in selected}
    titles = [story.title for story in grouped]
    assert titles[:3] == ["Otra IA", "Harness", "IA china"]  # frontier tech, score order
    assert set(titles[3:]) == {"Proveedor cloud", "Mundial", "Elecciones"}
    assert titles.index("Otra IA") < titles.index("Harness") < titles.index("Mundial")


def test_extractive_transcript_has_no_spoken_intro_numbers_or_coverage():
    selected = [_synthetic_story("Noticia uno"), _synthetic_story("Noticia dos")]
    transcript = build_transcript(date(2026, 9, 5), selected, 12, PodcastSettings())
    assert transcript == "Noticia uno. Medios: Fragmento sintético.\nNoticia dos. Medios: Fragmento sintético."
    for forbidden in ("Al día. Resumen", "extractivo", "Se incluyen", "Historia 1",
                      "Cobertura:", "sin fragmento", "Fin del resumen"):
        assert forbidden not in transcript


def test_editor_payload_has_only_the_four_allowed_fields():
    story = SimpleNamespace(title="T", snippet="S", publisher="P", category="tech",
                            sources=[{"url": "https://x", "publisher": "P", "title": "T"}])
    assert editor_story_payload([story, story]) == [
        {"id": 1, "title": "T", "snippet": "S", "publisher": "P"},
        {"id": 2, "title": "T", "snippet": "S", "publisher": "P"},
    ]


def test_build_editorial_transcript_only_joins_clean_segments():
    result = build_editorial_transcript([" Primera noticia. ", "", "Siguiente noticia. "])
    assert result == "Primera noticia.\nSiguiente noticia."


def test_validate_editor_segments_requires_each_id_exactly_once():
    first = "Primera noticia con suficientes palabras para la narración."
    second = "Segunda noticia con suficientes palabras para la narración."
    good = [{"id": 2, "text": second}, {"id": 1, "text": first}]
    assert validate_editor_segments(good, [1, 2]) == [first, second]


@pytest.mark.parametrize("segments", [
    [{"id": 1, "text": "Primera noticia."}, {"id": 1, "text": "Duplicada."}],  # duplicate
    [{"id": 1, "text": "Primera noticia."}],                                   # missing 2
    [{"id": 1, "text": "Primera noticia."}, {"id": 3, "text": "Otra."}],       # unknown 3
    [{"id": 1, "text": "   "}, {"id": 2, "text": "Segunda noticia."}],         # empty
    [{"id": 1, "text": "<think>x</think>"}, {"id": 2, "text": "Segunda noticia."}],  # tags
    [{"id": 1, "text": "**negrita**"}, {"id": 2, "text": "Segunda noticia."}],  # markdown
    [{"id": 1, "text": "x" * 3000}, {"id": 2, "text": "Segunda noticia."}],    # unbounded
    {"id": 1, "text": "Primera noticia."},                                    # not an array
    "not json",
])
def test_validate_editor_segments_rejects_incomplete_or_unsafe_output(segments):
    with pytest.raises(PodcastError):
        validate_editor_segments(segments, [1, 2])


def test_run_editor_uses_local_offline_subprocess_and_sends_four_fields(monkeypatch, tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    st = PodcastSettings(editor_python="fake-editor-python", editor_model=str(model),
                         editor_script=str(ROOT / "scripts" / "podcast-editor.py"))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        source = Path(command[command.index("--input") + 1])
        captured["payload"] = json.loads(source.read_text(encoding="utf-8"))
        out = Path(command[command.index("--output") + 1])
        out.write_text(json.dumps([{"id": p["id"], "text": "Segmento uno con palabras suficientes."}
                                   for p in captured["payload"]]), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("newsnest.podcast.subprocess.run", fake_run)
    texts = run_editor([_synthetic_story("Uno"), _synthetic_story("Dos")], stage, st)
    assert len(texts) == 2
    command = captured["command"]
    assert command[0] == "fake-editor-python"
    assert command[command.index("--model") + 1] == str(model)
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert all(set(p) == {"id", "title", "snippet", "publisher"} for p in captured["payload"])
    assert command[command.index("--provider") + 1] == "local"
    assert command[command.index("--api-model") + 1] == "gpt-6-luna"


def test_openai_editor_does_not_require_a_local_model(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        out = Path(command[command.index("--output") + 1])
        out.write_text(json.dumps([{"id": 1, "text": "Noticia suficiente para la narración del episodio."}]),
                       encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("newsnest.podcast.subprocess.run", fake_run)
    st = PodcastSettings(editor_python="fake-editor-python",
                         editor_script=str(ROOT / "scripts" / "podcast-editor.py"),
                         editor_provider="openai", editor_api_model="gpt-6-luna")
    assert run_editor([_synthetic_story("Uno")], tmp_path, st)
    command = captured["command"]
    assert command[command.index("--provider") + 1] == "openai"
    assert command[command.index("--api-model") + 1] == "gpt-6-luna"


def test_run_editor_failure_propagates(monkeypatch, tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    st = PodcastSettings(editor_python="fake-editor-python", editor_model=str(model),
                         editor_script=str(ROOT / "scripts" / "podcast-editor.py"))
    monkeypatch.setattr("newsnest.podcast.subprocess.run",
                        lambda *_a, **_k: SimpleNamespace(returncode=2, stdout="", stderr="bad output"))
    with pytest.raises(PodcastError, match="editor failed"):
        run_editor([_synthetic_story("Uno")], tmp_path, st)


def test_editorial_mode_publishes_segments_without_intro(env, cfg, conn, podcast_env, fake_tts,
                                                          fake_editor, monkeypatch, tmp_path):
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_MODEL", str(tmp_path))
    day = date(2026, 9, 5)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios",
                      url="https://medios.example/feed")]
    add_story(conn, cfg, title="Noticia principal", snippet="", published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    add_story(conn, cfg, title="Segunda nota", snippet="", published=datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc))
    metadata = generate(day, cfg, sources, settings(), conn)
    assert metadata["narration_mode"] == "editorial"
    assert metadata["transcript"] == "\n".join(
        f"Noticia {n}: {title}." for n, title in enumerate(["Noticia principal", "Segunda nota"], start=1))
    for forbidden in ("Resumen del", "Historia 1", "Cobertura:", "Fin del resumen"):
        assert forbidden not in metadata["transcript"]
    assert metadata["transcript"].startswith("Noticia 1:")
    assert latest_episode()["narration_mode"] == "editorial"
    assert len(fake_editor) == 1 and len(fake_tts) == 1


def test_configured_editor_failure_keeps_previous_episode(env, cfg, conn, podcast_env, fake_tts,
                                                          monkeypatch, tmp_path):
    day = date(2026, 9, 5)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios",
                      url="https://medios.example/feed")]
    add_story(conn, cfg, published=datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    st = settings()
    first = generate(day, cfg, sources, st, conn)
    assert first["narration_mode"] == "extractive"
    first_bytes = audio_path(day, first["version"]).read_bytes()

    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_MODEL", str(tmp_path))

    def boom(*_args, **_kwargs):
        raise PodcastError("editor failed")

    monkeypatch.setattr("newsnest.podcast.run_editor", boom)
    with pytest.raises(PodcastError, match="editor failed"):
        generate(day, cfg, sources, settings(), conn)
    assert latest_episode()["version"] == first["version"]
    assert audio_path(day, first["version"]).read_bytes() == first_bytes
    assert len(fake_tts) == 1  # no silent extractive fallback was synthesized
    assert not list((podcasts_root() / ".staging").glob("*"))


def _capture_tts_command(monkeypatch):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
        sidecar = Path(command[command.index("--sidecar") + 1])
        sidecar.write_text(json.dumps({"duration_seconds": 3.0}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("newsnest.podcast.subprocess.run", fake_run)
    return captured


def test_run_tts_forwards_tempo_seed_and_paired_reference(monkeypatch, tmp_path):
    captured = _capture_tts_command(monkeypatch)
    st = PodcastSettings(python="fake-python", model="/model", script=str(ROOT / "scripts" / "podcast-tts.py"),
                         ref_audio="/voices/ref.wav", ref_text_file="/voices/ref.txt",
                         pronunciations_file="/voices/pronunciaciones.json", tempo=1.25, seed=7)
    assert run_tts("Narración de prueba.", tmp_path / "episode.mp3", st) == 3.0
    command = captured["command"]
    assert command[command.index("--tempo") + 1] == "1.25"
    assert command[command.index("--seed") + 1] == "7"
    assert command[command.index("--ref-audio") + 1] == "/voices/ref.wav"
    assert command[command.index("--ref-text-file") + 1] == "/voices/ref.txt"
    assert command[command.index("--pronunciations-file") + 1] == "/voices/pronunciaciones.json"


def test_run_tts_skips_unpaired_reference(monkeypatch, tmp_path):
    captured = _capture_tts_command(monkeypatch)
    st = PodcastSettings(python="fake-python", model="/model", script=str(ROOT / "scripts" / "podcast-tts.py"),
                         ref_audio="/voices/ref.wav")
    run_tts("Narración de prueba.", tmp_path / "episode.mp3", st)
    command = captured["command"]
    assert "--ref-audio" not in command and "--ref-text-file" not in command
    assert "--tempo" in command and "--seed" in command


def test_codex_editor_argv_uses_explicit_binary(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        out = Path(command[command.index("--output") + 1])
        out.write_text(json.dumps([{"id": 1, "text": "Noticia suficiente para el episodio."}]),
                       encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("newsnest.podcast.subprocess.run", fake_run)
    st = PodcastSettings(editor_python="fake-editor-python",
                         editor_script=str(ROOT / "scripts" / "podcast-editor.py"),
                         editor_provider="codex", editor_codex_binary="my-codex")
    assert run_editor([_synthetic_story("Uno")], tmp_path, st)
    command = captured["command"]
    assert command[command.index("--provider") + 1] == "codex"
    assert command[command.index("--codex-binary") + 1] == "my-codex"


def test_editor_settings_read_from_environment(podcast_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_MODEL", str(tmp_path))
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_PYTHON", "editor-python")
    monkeypatch.setenv("NEWSNEST_PODCAST_REF_AUDIO", "/a.wav")
    monkeypatch.setenv("NEWSNEST_PODCAST_REF_TEXT_FILE", "/a.txt")
    monkeypatch.setenv("NEWSNEST_PODCAST_TEMPO", "1.5")
    monkeypatch.setenv("NEWSNEST_PODCAST_SEED", "42")
    monkeypatch.setenv("NEWSNEST_PODCAST_PRONUNCIATIONS_FILE", "/pronunciaciones.json")
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_PROVIDER", "openai")
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_API_MODEL", "gpt-6-luna")
    st = settings()
    assert st.editor_model == str(tmp_path)
    assert st.editor_python == "editor-python"
    assert (st.ref_audio, st.ref_text_file) == ("/a.wav", "/a.txt")
    assert st.pronunciations_file == "/pronunciaciones.json"
    assert st.tempo == 1.5 and st.seed == 42
    assert st.editor_provider == "openai" and st.editor_api_model == "gpt-6-luna"
    assert st.editor_script.endswith("podcast-editor.py")


def test_public_episode_narration_mode_fallback_and_preservation(env):
    day = date(2026, 9, 5)
    legacy = episode_dir(day) / "episode.mp3"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
    base = {"day": day.isoformat(), "duration_seconds": 12.5, "item_count": 1, "total_items": 1,
            "audio_url": "/reels/podcast/2026-09-05/audio", "transcript": "synthetic", "sources": [],
            "generated_at": "2026-09-05T06:04:12+00:00"}
    manifest = podcasts_root() / "manifest.json"
    manifest.write_text(json.dumps(base), encoding="utf-8")
    assert latest_episode()["narration_mode"] == "extractive"
    for invalid in ("editorial!", "", None):
        manifest.write_text(json.dumps({**base, "narration_mode": invalid}), encoding="utf-8")
        assert latest_episode()["narration_mode"] == "extractive"
    version = str(uuid.uuid4())
    (episode_dir(day) / f"episode-{version}.mp3").write_bytes(legacy.read_bytes())
    manifest.write_text(json.dumps({**base, "version": version, "narration_mode": "editorial"}),
                        encoding="utf-8")
    assert latest_episode()["narration_mode"] == "editorial"


def test_editorial_metadata_labels_context_sources_without_changing_item_count(
        env, cfg, conn, podcast_env, fake_tts, fake_editor, monkeypatch, tmp_path):
    day = date(2026, 9, 15)
    sources = [Source(id="medios", type="rss", category="tech", name="Medios",
                      url="https://medios.example/feed")]
    add_story(conn, cfg, title="Mercado Libre invierte en logística",
              published=datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc))
    add_story(conn, cfg, title="Mercado Libre abre planta",
              published=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setenv("NEWSNEST_PODCAST_EDITOR_MODEL", str(tmp_path))
    metadata = generate(day, cfg, sources, settings(), conn)
    assert metadata["item_count"] == 1  # context does not change the fresh count
    labeled = [s for s in metadata["sources"] if s.get("context")]
    assert labeled and labeled[0]["title"] == "Mercado Libre abre planta"
    assert latest_episode()["sources"][-1]["context"] is True
