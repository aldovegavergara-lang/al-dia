"""Bounded historical context for the podcast editor, read only from the local database.

A past item is related when it shares at least two meaningful title components (named entities
or non-generic keywords) with today's story, inside a 14-day window ending at the target day.
Trailing publisher branding ("... - Daily Market") is stripped before matching so two items from
the same outlet are not related just because of the outlet's name; a single broad entity such as
a shared brand is never enough. The reader's category is deliberately ignored: a whole section is
not "context". At most two items per story are returned, with title, snippet, publication date,
URL and publisher so the editor can explain a continuing story without inventing background from
stale model memory.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from zoneinfo import ZoneInfo

from newsnest.db import iso

DEFAULT_LOOKBACK_DAYS = 14
MAX_CONTEXT = 2
DEFAULT_TIMEZONE = "America/Santiago"

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]{4,}")
_ENTITY_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][A-Za-záéíóúñ]{3,}")

# Capitalized but generic openers are not real named entities; a match on them alone is noise.
# Spanish headlines routinely start with a capitalised verb, and English feeds title-case ordinary
# words. Both are filtered here so only conservative names (brands, acronyms, multiword names) or
# two meaningful keywords can relate two stories. Lowercased members also join _STOPWORDS, so only
# words that are never meaningful keywords belong in this set.
_GENERIC_ENTITIES = frozenset({
    "Gobierno", "Gubernamental", "Nuevo", "Nueva", "Nuevos", "Nuevas", "Primero", "Primera",
    "Ultimo", "Último", "Última", "Ministro", "Ministerio", "Models", "Modelos", "Model",
    "Segun", "Según", "Tras", "Desde", "Hasta", "Entre", "Caso", "Casos", "Semana", "Meses",
    # Reporting verbs: a shared "Anunció"/"Reportó" is not a continuing story.
    "Anunció", "Anuncio", "Anunciaron", "Reportó", "Reporte", "Reportaron", "Informó",
    "Informe", "Informaron", "Confirmó", "Confirmaron", "Reveló", "Revelaron", "Detalló",
    "Detallaron", "Agregó", "Agregaron", "Explicó", "Explicaron", "Indicó", "Indicaron",
    "Señaló", "Señalaron", "Sostuvo", "Sostuvieron", "Aseguró", "Aseguraron", "Mencionó",
    "Mencionaron", "Estimó", "Estimaron", "Pronosticó", "Denunció", "Criticó", "Defendió",
    "Publicó", "Publicaron", "Presentó", "Presentaron", "Lanzó", "Lanzaron", "Inició",
    "Comenzó", "Continuará", "Busca", "Buscan", "Pide", "Piden", "Propone", "Llega",
    "Llegan", "Sube", "Suben", "Cae", "Caen", "Afirma", "Afirman", "Calcula", "Calculan",
    # Generic tech/news words (never brands).
    "Tecnología", "Tecnologías", "Digital", "Digitales", "Plataforma", "Plataformas",
    "Aplicación", "Aplicaciones", "Software", "Hardware", "Sistemas", "Servicios",
    "Productos", "Datos", "Seguridad", "Privacidad", "Negocios", "Industria", "Compañía",
    "Gobernanza", "Regulación", "Investigación", "Estudio", "Estudios", "Resumen",
    "Actualidad", "Últimas", "Ultimas", "Nacional", "Internacional", "Exterior", "Mundo",
    "Sociedad", "Economía", "Economia", "Política", "Politica", "Deportes", "Ciencia",
    "Salud", "Cultura", "Opinión", "Opinion", "Editorial", "Redacción", "Redaccion",
    "Santiago", "País", "Pais", "Región", "Region", "Comunas", "Ciudad", "Pronóstico",
    "Pronostico", "Previsión", "Prevision", "Tiempo", "Clima",
    # Ordinary English TitleCase words from international feeds.
    "Latest", "Update", "Updated", "Report", "Reports", "Tech", "Technology", "Breaking",
    "Insider", "Daily", "Weekly", "Exclusive", "Live", "Guide", "News",
})

_STOPWORDS = frozenset("""
    sobre segun según ante bajo cuando donde mientras porque para desde hasta tras como
    este esta estos estas más menos pero todo tras seria serán fueron siendo hay haber
    durante mediante algunos algunas otro otra otros otras antes después apenas incluso
    the and for with from that this into after before over under its has have was were will
    said says new more than also not but their they them then there here what when where who
    why how all can may its
""".split()) | {word.lower() for word in _GENERIC_ENTITIES}


def _clean(text: str | None, limit: int) -> str:
    text = unescape(_TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:limit].rstrip()


def _keywords(title: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(title or "") if word.lower() not in _STOPWORDS}


def _entities(title: str) -> set[str]:
    return {word for word in _ENTITY_RE.findall(title or "")
            if word.lower() not in _STOPWORDS and word not in _GENERIC_ENTITIES}


def _components(title: str) -> set[str]:
    """Lowercased meaningful components: named entities together with non-generic keywords."""
    return {word.lower() for word in _entities(title)} | _keywords(title)


def _strip_brand(title: str, brands: set[str]) -> str:
    """Remove a trailing publisher brand for matching only; the displayed title keeps it.

    Headlines often append the outlet ("Historia - Daily Market", "Nota | El Diario"). That
    suffix is presentation, not evidence that two unrelated stories continue one another. Only a
    brand known from an item's publisher, source name or the target's publisher is removed, and
    only when it follows a separator, so genuinely meaningful closing words are preserved.
    """
    result = (title or "").strip()
    for brand in sorted((b.strip() for b in brands if b and b.strip()), key=len, reverse=True):
        result = re.sub(r"\s*[-|–—:]\s*" + re.escape(brand) + r"\s*$", "", result,
                        flags=re.IGNORECASE).rstrip()
    return result.strip()


def related_context(conn, story, day: date, *, tz: ZoneInfo | None = None,
                    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                    max_items: int = MAX_CONTEXT) -> list[dict]:
    """At most ``max_items`` prior stories related to ``story`` in [day-N, day), newest first."""
    tz = tz or ZoneInfo(DEFAULT_TIMEZONE)
    end = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
    start = datetime.combine(day - timedelta(days=max(1, lookback_days)), time.min, tz).astimezone(timezone.utc)
    rows = conn.execute(
        """SELECT i.title, i.snippet, i.url, i.published, i.publisher, i.source_id
           FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.published >= ? AND i.published < ?
             AND i.hidden_at IS NULL AND (i.prefilter IS NULL OR i.prefilter != 'muted')
           ORDER BY i.published DESC""",
        (iso(start), iso(end))).fetchall()
    names = {row["source_id"]: row["name"] for row in conn.execute(
        "SELECT source_id, name FROM library_sources")}
    own_urls = {source.get("url") for source in getattr(story, "sources", [])}
    title = getattr(story, "title", "") or ""
    # Publisher branding is presentation, not signal: strip it from both sides before matching.
    brands = {str(getattr(story, "publisher", "") or "").strip()}
    brands |= {str(row["publisher"] or "").strip() for row in rows}
    brands |= {str(names.get(row["source_id"]) or "").strip() for row in rows}
    brands.discard("")
    target_components = _components(_strip_brand(title, brands))
    if not target_components:
        return []
    entries: list[dict] = []
    for row in rows:
        if row["url"] in own_urls:
            continue
        candidate = row["title"] or ""
        # Require two shared meaningful components; one capitalized word (a broad brand or a
        # publisher suffix) is not enough to link two stories. Zero context is the safe result.
        if len(_components(_strip_brand(candidate, brands)) & target_components) < 2:
            continue
        entries.append({
            "title": _clean(candidate, 300),
            "snippet": _clean(row["snippet"], 500),
            "published": str(row["published"] or ""),
            "url": str(row["url"] or ""),
            "publisher": _clean(row["publisher"] or names.get(row["source_id"])
                                or row["source_id"], 160),
        })
        if len(entries) >= max(1, max_items):
            break
    return entries
