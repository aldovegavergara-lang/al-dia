"""Explicit, reversible tastes for Al día. Local title/source matching; no model calls."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from newsnest.digest import make_cards
from newsnest.textmatch import SEED_STOPWORDS, fold, title_tokens

_FILLER = title_tokens(' '.join(SEED_STOPWORDS) + '''
 de del la las los el una uno unos unas para por que como cuando donde quien esto esta estos
 estas ese esa esos esas sobre entre desde hasta tras ante bajo contra hacia segun sin con
 son ser sera sido tiene tienen tener puede pueden pero porque mas menos muy todo todos
 toda todas otro otra otros otras este esta sus nos noticia noticias nuevo nueva nuevos
 nuevas hoy ayer ahora ultimo ultima primeros primera dice dijo anuncia presenta conoce
''')


def card_rating(card):
    """Latest explicit vote wins across all versions of a story."""
    rated = [m.row for m in [card.lead, *card.others] if m.row['rating'] in (-1, 1)]
    return max(rated, key=lambda r: (r['rated_at'] or '', r['id']))['rating'] if rated else 0


@dataclass
class Taste:
    tokens: set
    sources: set
    publishers: set
    categories: set
    value: int


def _taste(card):
    rows = [m.row for m in [card.lead, *card.others]]
    return Taste(set.union(*(title_tokens(r['title']) - _FILLER for r in rows)),
                 {r['source_id'] for r in rows},
                 {fold(r['publisher']) for r in rows if r['publisher']},
                 {r['category'] for r in rows}, card_rating(card))


def load_tastes(conn, cfg):
    # Group duplicate coverage before learning: ten outlets on one story still count as one vote.
    rows = conn.execute('SELECT * FROM items WHERE rating IN (-1,1) ORDER BY id').fetchall()
    return [_taste(c) for c in make_cards(rows, cfg, {})]


def preference_multiplier(card, tastes):
    if not tastes:
        return 1.0
    target = _taste(card)
    similar = []
    sources, publishers, categories = [], [], []
    for taste in tastes:
        common = target.tokens & taste.tokens
        if common:
            similarity = len(common) / sqrt(len(target.tokens) * len(taste.tokens))
            similar.append((similarity, taste.value))
        if target.sources & taste.sources:
            sources.append(taste.value)
        if target.publishers & taste.publishers:
            publishers.append(taste.value)
        if target.categories & taste.categories:
            categories.append(taste.value)
    similar.sort(reverse=True)
    similar = similar[:5]
    topical = sum(s * v for s, v in similar) / max(1, sum(s for s, _ in similar))
    def mean(values):
        return sum(values) / len(values) if values else 0
    # A broad section is a weak signal; matching titles carries most of the adjustment.
    shift = (.65 * topical + .15 * mean(sources) + .1 * mean(publishers)
             + .05 * mean(categories) + .3 * target.value)
    return max(.25, min(1.75, 1 + shift))
