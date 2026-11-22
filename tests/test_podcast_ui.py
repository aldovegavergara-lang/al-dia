"""Synthetic render checks for the first-card daily podcast frontend.

These tests do not need a generated audio file or the collector. They render the
reader template with the same context shape the route supplies and inspect the
podcast card before it reaches a browser.
"""

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "newsnest" / "templates"


def _render(podcast, *, today=date(2026, 11, 22), cards=None, template="reels.html", **extra):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"])
    )
    row = NS(
        title="Noticia de prueba",
        publisher="Medio",
        published="2026-11-22T09:00:00+00:00",
        snippet="Resumen",
        fetched_at="2026-11-22T10:00:00+00:00",
        category="tech",
        url="https://example.com/story",
    )
    card = NS(
        lead=NS(id=1, row=row, source_name="Medio"),
        ids="1",
        image=None,
        summary_member=None,
        watches=[],
        others=[],
        saved=False,
        read=False,
    )
    context = dict(
        cards=[card] if cards is None else cards,
        podcast=podcast,
        today=today,
        timedelta=timedelta,
        request=NS(url=NS(path="/reels")),
        status=NS(sources=[], banner=None),
        cfg=NS(schedules=NS(tech_minutes=30), all_sections=[]),
        repo_url="https://example.com",
        localtime=lambda *args, **kwargs: "22/11 · 06:00",
        initials=lambda value: "M",
        publication_age=lambda value: "Hace 1 hora",
        key_points=lambda row: [],
        section_labels={},
        card_rating=lambda card: 0,
        reader_url=lambda **kwargs: "/reels",
        view_url=lambda view: "/reels",
        counts=NS(pending=0),
        topic_rules=[],
        article_filters=NS(
            topic="all", period=0, reaction="all", exclude="", bookmarked=False
        ),
        library_sources=[],
        saved_views=[],
        origins={},
        card_topics={},
        total=1,
        next_after=None,
        hours=24,
        query="",
        order="oldest",
        selected="all",
        source="",
        saved=False,
        view="pending",
        version_id=1,
        has_filters=False,
    )
    context.update(extra)
    return env.get_template(template).render(**context)


def _podcast_html(html):
    start = html.index("data-podcast-reel")
    end = html.index("</article>", start)
    return html[start:end]


def _podcast(**overrides):
    data = dict(
        day="2026-11-21",
        title="Resumen del sábado",
        duration_seconds=754,
        item_count=7,
        total_items=42,
        audio_url="https://audio.example/episode.mp3",
        transcript="Línea uno\nLínea dos",
        sources=[
            {
                "title": "Una fuente",
                "url": "https://example.com/source",
                "publisher": "Medio",
            }
        ],
        generated_at="2026-11-22T06:00:00+00:00",
    )
    data.update(overrides)
    return data


def test_podcast_is_first_card_with_accessible_native_audio():
    html = _render(_podcast())
    podcast = _podcast_html(html)
    assert html.index("data-podcast-reel") < html.index('data-id="1"')
    assert "Tu resumen de ayer" in podcast
    assert "12 min 34 s" in podcast
    assert "7 noticias de 42 recopiladas" in podcast
    assert 'preload="none"' in podcast
    assert "controls" in podcast
    assert "autoplay" not in podcast
    assert 'src="https://audio.example/episode.mp3"' in podcast
    assert "data-podcast-toggle" in podcast
    assert "Leer transcripción y fuentes" in podcast


def test_podcast_never_carries_news_ids():
    podcast = _podcast_html(_render(_podcast()))
    assert "data-id" not in podcast
    assert "data-ids" not in podcast
    assert "data-read" not in podcast
    assert "data-save" not in podcast


def test_old_podcast_uses_relative_days_instead_of_claiming_yesterday():
    podcast = _podcast_html(_render(_podcast(day="2026-11-19")))
    assert "Tu resumen de ayer" not in podcast
    assert "Tu resumen de hace 3 días" in podcast


def test_podcast_metadata_is_escaped():
    podcast = _podcast_html(
        _render(
            _podcast(
                title="<img src=x onerror=alert(1)>",
                transcript="<script>alert(1)</script>",
                sources=[
                    {
                        "title": "<b>Fuente</b>",
                        "url": "https://example.com/a?x=1&y=2",
                        "publisher": "<i>Medio</i>",
                    }
                ],
            )
        )
    )
    assert "<script>alert(1)</script>" not in podcast
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in podcast
    assert "<img src=x" not in podcast
    assert "Titulares y extractos · voz local" in podcast
    assert "x=1&amp;y=2" in podcast
    assert "&lt;b&gt;Fuente&lt;/b&gt;" in podcast


def test_without_a_podcast_the_reader_is_unchanged():
    html = _render(None)
    assert "data-podcast-reel" not in html
    assert "Tu resumen de ayer" not in html
    assert 'data-id="1"' in html


def test_podcast_javascript_guards_native_audio_and_resumes_locally():
    script = (ROOT / "newsnest" / "static" / "reels.js").read_text()
    assert "data-podcast-reel" in script
    assert "localStorage" in script
    assert "HTMLAudioElement" in script
    assert (
        "target.closest('input, textarea, select, button, audio, video, summary"
        in script
    )
    assert "if (!button.dataset.ids) return;" in script


def test_long_podcast_details_have_a_focusable_scroll_region():
    podcast = _podcast_html(_render(_podcast(sources=[{"title": "Fuente " + str(n), "url": "https://example.com/" + str(n), "publisher": "Medio"} for n in range(30)])))
    assert 'class="podcast-details-content" tabindex="0" role="region"' in podcast
    assert 'Fuente 29' in podcast
    assert 'data-relative-time data-time-label="Generación"' in podcast


def test_manage_status_hides_diagnostics_and_handles_delay():
    html = _render(None, template="reels_manage.html", podcast_enabled=True, podcast_max_stories=25,
                   podcast_status={"state":"error", "is_late":True, "error":"SECRET_PRIVATE_PATH"})
    assert "El resumen de ayer se está retrasando" in html
    assert "SECRET_PRIVATE_PATH" not in html
    assert "03:00" in html and "04:00" in html
    assert "Hasta 25 historias" in html
    ready = _render(None, template="reels_manage.html", podcast_enabled=True, podcast_max_stories=0,
                    podcast_status={"state":"ready", "last_success":"2026-11-22T07:00:00+00:00"})
    assert "Tu último episodio está listo" in ready
    assert "todas las historias elegibles" in ready
    assert "hace 1 hora" in ready
