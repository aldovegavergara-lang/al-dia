"""Shared filters and persistent saved views for the reader and its read-only API."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

from newsnest.db import dumps, get_meta, loads, parse_iso, set_meta, transaction, utcnow
from newsnest.reactions import card_rating
from newsnest.textmatch import fold
from newsnest.topics import content_text, phrases

VIEW_KEY = 'aldia.saved_views.v1'
FILTER_KEYS = ('view','f','source','q','hours','order','topic','period','reaction','exclude','bookmarked')


@dataclass
class ArticleFilter:
    topic: str = 'all'
    period: int = 0
    reaction: str = 'all'
    exclude: str = ''
    bookmarked: bool = False
    query: str = ''

    def __post_init__(self):
        self.topic = self.topic.strip()[:100] or 'all'
        self.period = self.period if self.period in (24,72,168,720) else 0
        self.reaction = self.reaction if self.reaction in ('liked','disliked','unrated') else 'all'
        self.exclude = self.exclude.strip()[:1000]
        self.query = self.query.strip()[:200]

    @property
    def active(self):
        return self.topic != 'all' or self.period or self.reaction != 'all' or self.exclude or self.bookmarked or self.query


def search_matches(query, text):
    """AND terms; quoted phrases; OR alternatives; -term exclusions. Tolerate unfinished quotes."""
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.replace('"',' ').replace("'",' ').split()
    if not tokens:
        return True
    groups = [[]]
    for token in tokens:
        if token == 'OR' and groups[-1]:
            groups.append([])
        else:
            groups[-1].append(token)
    groups = [g for g in groups if g]
    return any(all((fold(t[1:]) not in text) if t.startswith('-') and len(t)>1
                   else fold(t) in text for t in g) for g in groups)


def matches_card(card, labels, filters, *, source_names=(), now=None):
    if filters.topic == 'unclassified':
        if labels:
            return False
    elif filters.topic != 'all' and filters.topic not in {t['id'] for t in labels}:
        return False
    rating = card_rating(card)
    if filters.reaction != 'all' and rating != {'liked':1,'disliked':-1,'unrated':0}[filters.reaction]:
        return False
    if filters.bookmarked and not card.saved:
        return False
    if filters.period:
        now = now or utcnow()
        published = [parse_iso(m.row['published']) for m in [card.lead,*card.others] if m.row['published']]
        if not any(now-timedelta(hours=filters.period) <= p <= now+timedelta(minutes=5) for p in published):
            return False
    text = content_text(card) + ' ' + fold(' '.join(source_names))
    if any(fold(term) in text for term in phrases(filters.exclude)):
        return False
    return search_matches(filters.query, text)


def clean_view_params(raw):
    params = {k:str(raw[k])[:1000] for k in FILTER_KEYS if k in raw}
    view = params.get('view','pending')
    params['view'] = view if view in ('pending','latest','read','saved','all') else 'pending'
    for key, allowed, default in [('hours',(24,72),24),('period',(0,24,72,168,720),0),('bookmarked',(0,1),0)]:
        try:
            value = int(params.get(key,default))
        except (ValueError,TypeError):
            value = default
        params[key] = str(value if value in allowed else default)
    advanced = ArticleFilter(topic=params.get('topic','all'),period=int(params['period']),
                             reaction=params.get('reaction','all'),exclude=params.get('exclude',''),
                             query=params.get('q',''))
    params.update(topic=advanced.topic, reaction=advanced.reaction, exclude=advanced.exclude, q=advanced.query)
    params['order'] = params.get('order') if params.get('order') in ('oldest','newest') else 'oldest' if params['view']=='pending' else 'newest'
    return params


def saved_views(conn):
    return loads(get_meta(conn,VIEW_KEY), [])


def save_view(conn, name, raw):
    name = name.strip()
    if not name or len(name)>60:
        raise ValueError('Escribe un nombre de hasta 60 caracteres para la vista.')
    params = clean_view_params(raw)
    with transaction(conn):
        views = saved_views(conn)
        if len(views)>=24:
            raise ValueError('Puedes guardar hasta 24 vistas. Quita una antes de añadir otra.')
        if any(fold(v['name'])==fold(name) for v in views):
            raise ValueError('Ya hay una vista con ese nombre.')
        view = {'id':uuid4().hex[:16], 'name':name, 'params':params}
        views.append(view)
        set_meta(conn,VIEW_KEY,dumps(views))
    return view


def remove_view(conn, view_id):
    with transaction(conn):
        set_meta(conn,VIEW_KEY,dumps([v for v in saved_views(conn) if v['id']!=view_id]))


def view_url(view):
    return '/reels?' + urlencode(clean_view_params(view['params']))
