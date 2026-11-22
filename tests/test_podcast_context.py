"""Synthetic tests for bounded historical context: date cutoff, relevance and per-story cap."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from newsnest.podcast import Story
from newsnest.podcast_context import related_context
from tests.test_podcast import add_story

DAY = date(2026, 9, 15)
TZ = ZoneInfo("America/Santiago")


def _story(title: str) -> Story:
    return Story(title=title, snippet="", category="tech",
                 published=datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc),
                 score=0.0, publisher="Medios", sources=[])


def _published(days_before: int) -> datetime:
    # Noon Santiago on a closed civil day, comfortably inside the UTC window.
    return (datetime.combine(DAY - timedelta(days=days_before), datetime.min.time(), TZ)
            .replace(hour=12).astimezone(timezone.utc))


def test_context_uses_14_day_cutoff_and_skips_unrelated_stories(env, cfg, conn):
    add_story(conn, cfg, title="Mercado Libre abre planta", published=_published(20))
    add_story(conn, cfg, title="Mercado Libre abre planta", published=_published(2))
    add_story(conn, cfg, title="Pronóstico de lluvia mañana", published=_published(1), category="weather")
    context = related_context(conn, _story("Mercado Libre invierte en logística"), DAY, tz=TZ)
    assert len(context) == 1
    assert context[0]["title"] == "Mercado Libre abre planta"
    assert set(context[0]) == {"title", "snippet", "published", "url", "publisher"}


def test_context_matches_two_non_generic_keywords_not_whole_category(env, cfg, conn):
    add_story(conn, cfg, title="Chile debate ley de inteligencia artificial",
              published=_published(1), category="world")
    add_story(conn, cfg, title="Otro tema de tecnología sin relación",
              published=_published(1), category="tech")
    context = related_context(conn, _story("Nueva ley de inteligencia artificial"), DAY, tz=TZ)
    assert len(context) == 1
    assert "inteligencia artificial" in context[0]["title"]


def test_context_is_capped_at_two_and_newest_first(env, cfg, conn):
    for days in (1, 2, 3, 4):
        add_story(conn, cfg, title="Mercado Libre abre planta", published=_published(days))
    context = related_context(conn, _story("Mercado Libre invierte"), DAY, tz=TZ)
    assert len(context) == 2
    assert context[0]["published"] > context[1]["published"]


def test_context_respects_hidden_and_muted_preferences(env, cfg, conn):
    add_story(conn, cfg, title="Mercado Libre abre planta", published=_published(1),
              hidden_at=datetime(2026, 9, 14, tzinfo=timezone.utc))
    add_story(conn, cfg, title="Mercado Libre invierte", published=_published(2),
              prefilter="muted", score_status="muted")
    assert related_context(conn, _story("Mercado Libre abre planta"), DAY, tz=TZ) == []


def test_generic_capitalized_words_do_not_create_false_matches(env, cfg, conn):
    add_story(conn, cfg, title="Nuevo Gobierno anuncia medidas", published=_published(1))
    assert related_context(conn, _story("Nuevo Models de Gobierno"), DAY, tz=TZ) == []


def test_context_excludes_the_storys_own_urls(env, cfg, conn):
    add_story(conn, cfg, title="Mercado Libre abre planta", published=_published(1),
              url="https://medios.example/mismo")
    story = _story("Mercado Libre invierte")
    story.sources = [{"title": "Mercado Libre abre planta", "url": "https://medios.example/mismo",
                      "publisher": "Medios"}]
    assert related_context(conn, story, DAY, tz=TZ) == []


# --- Publisher branding and single broad entities must not create context ----------------------

def test_context_does_not_match_only_on_publisher_suffix(env, cfg, conn):
    add_story(conn, cfg, title="Toolmaker outage warning - Daily Market",
              source_name="Daily Market", published=_published(1))
    context = related_context(conn, _story("Retailer stock options - Daily Market"), DAY, tz=TZ)
    assert context == [], f"publisher suffix leaked as context: {context}"


def test_context_does_not_match_only_on_one_broad_entity(env, cfg, conn):
    add_story(conn, cfg, title="Amazon laptop discounts", published=_published(1))
    context = related_context(conn, _story("Amazon cloud coding guide"), DAY, tz=TZ)
    assert context == [], f"single broad entity leaked as context: {context}"


def test_context_ignores_brand_suffix_for_matching_but_keeps_displayed_title(env, cfg, conn):
    add_story(conn, cfg, title="Mercado Libre abre planta - Daily Market",
              source_name="Daily Market", published=_published(1))
    context = related_context(conn, _story("Mercado Libre invierte"), DAY, tz=TZ)
    assert len(context) == 1
    assert context[0]["title"] == "Mercado Libre abre planta - Daily Market"
