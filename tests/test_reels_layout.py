"""Layout contracts for the full-height reader and its unified right action rail.

Render-only checks: no audio file, collector or browser is needed. The visual
geometry (no overlap at 320x568, 390x844, 844x390 and desktop) is verified
separately with a synthetic browser harness.
"""

from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_podcast_ui import _podcast, _podcast_html, _render

ROOT = Path(__file__).resolve().parent.parent


def _card_with_other_sources():
    row = NS(title="Otro titular", publisher="Otro medio", published=None, snippet="Texto",
             fetched_at="2026-11-22T10:00:00+00:00", category="tech", url="https://example.com/other")
    return NS(lead=NS(id=2, row=row, source_name="Otro medio"), ids="2", image=None,
              summary_member=None, watches=[],
              others=[NS(id=3, source_name="Fuente extra", row=row)], saved=False, read=False)


def test_existing_article_controls_live_in_one_rail():
    html = _render(None)
    rail_start = html.index('class="story-rail"')
    rail = html[rail_start:html.index("</article>", rail_start)]

    # The controls that already existed moved into one rail; no new clutter.
    assert 'data-reaction="1"' in rail and 'data-reaction="-1"' in rail
    for hook in ("data-save", "data-read", "data-share", 'class="rail-source"'):
        assert hook in rail, hook
    for label in (
        "Me interesa", "No me interesa", "Guardar noticia", "Marcar como leída",
        "Compartir noticia", "Leer la noticia en la fuente",
    ):
        assert label in rail, label
    assert "aria-pressed=" in rail and "data-ids=" in rail
    assert "data-hide" not in rail and "reaction-reset" not in rail

    # The copy column keeps only the story itself.
    column = html[html.index('class="story-copy"'):rail_start]
    for hook in ("data-reaction=", "data-save", "data-read", "data-share", "source-link"):
        assert hook not in column, hook

    # One collapsed "Detalles" control holds classification, origin and other sources.
    meta = html[html.index('<details class="story-details"'):html.index('class="story-copy"')]
    assert "<summary>Detalles</summary>" in meta
    assert '<details class="story-details" open' not in meta
    assert "classification-info" in meta and "library-origin" in meta
    assert "¿Por qué estos temas?" in meta
    assert "<details" not in meta.split("</details>")[0].replace('<details class="story-details">', "", 1)

    extra = _render(None, cards=[_card_with_other_sources()])
    extra_meta = extra[extra.index('<details class="story-details"'):extra.index('class="story-copy"')]
    assert "other-sources" in extra_meta and "<details" not in extra_meta.replace('<details class="story-details">', "", 1)


def test_podcast_labels_follow_narration_mode():
    editorial = _podcast_html(_render(_podcast(narration_mode="editorial")))
    assert "RESUMEN DEL DÍA" in editorial
    assert "Resumen narrado en español · voz local" in editorial
    assert "TITULARES Y EXTRACTOS" not in editorial

    legacy = _podcast_html(_render(_podcast()))
    assert "TITULARES Y EXTRACTOS" in legacy
    assert "Titulares y extractos · voz local" in legacy
    assert "RESUMEN DEL DÍA" not in legacy
    assert "Resumen narrado en español · voz local" not in legacy


def test_stylesheet_and_script_keep_the_layout_contract():
    css = (ROOT / "newsnest" / "static" / "reels.css").read_text()
    assert ".reel-feed{height:100dvh" in css
    assert ".story-copy{flex:1 1 auto;min-height:0;max-height:none" in css
    assert ".story-rail{position:absolute" in css
    assert "max-height:calc(100dvh - 76px - env(safe-area-inset-top)" in css
    assert "top:calc(64px + env(safe-area-inset-top))" in css
    assert "overflow-y:auto;scrollbar-width:none" in css
    assert "width:44px;min-width:44px;height:44px;min-height:44px" in css
    assert ".story-details>summary" in css
    assert ".reader-menu-toggle [data-menu-label]{display:none}" in css

    script = (ROOT / "newsnest" / "static" / "reels.js").read_text()
    assert "'hide' in button.dataset" not in script
    assert "data-podcast-reel" in script
