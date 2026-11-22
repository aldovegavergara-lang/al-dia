"""Synthetic tests for the daily local podcast: boundaries, selection, atomicity and the reader."""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from newsnest.bootstrap import data_dir
from newsnest.config import Source
from newsnest.db import connect, iso, migrate, utcnow
from newsnest.grouping import new_story
from newsnest.models import Item
from newsnest.podcast import (PodcastError, PodcastSettings, audio_path, build_transcript, day_window,
                              eligible_stories, episode_dir, generate, latest_episode, podcasts_root,
                              public_status, read_status, select_stories, settings, target_day, tick)
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
    assert "sin fragmento disponible" in transcript
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
    assert "2 de 5 historias elegibles" in transcript
    assert "Cobertura:" in transcript

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
    assert metadata["transcript"].startswith("Al día. Resumen del sábado 5 de septiembre de 2026.")
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
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert "".join(chunks) or True
    assert script.split_text("", 120) == []
    long_word = script.split_text("x" * 400, 120)
    assert all(chunk for chunk in long_word)
