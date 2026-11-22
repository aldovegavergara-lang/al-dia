"""Persistent reading inbox, independent of the news feed's freshness/ranking limits."""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from newsnest.db import iso, utcnow
from newsnest.digest import make_cards
from newsnest.feed import story_hidden
from newsnest.reactions import card_rating
from newsnest.reader_filters import ArticleFilter, matches_card
from newsnest.topics import TopicClassifier, topic_rules


def remember_sources(conn, sources, now=None):
    stamp = iso(now or utcnow())
    for source in sources:
        conn.execute('''INSERT INTO library_sources (source_id,name,url,category,first_seen,last_seen)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                        name=excluded.name,url=excluded.url,category=excluded.category,last_seen=excluded.last_seen''',
                     (source.id, source.name or source.id, source.url, source.category, stamp, stamp))


def remember_discovery(conn, item_id, source_id, now):
    stamp = iso(now)
    conn.execute('''INSERT INTO item_sources (item_id,source_id,first_seen,last_seen) VALUES (?,?,?,?)
                    ON CONFLICT(item_id,source_id) DO UPDATE SET last_seen=excluded.last_seen''',
                 (item_id, source_id, stamp, stamp))


def initialize_library(conn, sources):
    """Keep the original source of previously collected articles; never invent past matches."""
    remember_sources(conn, sources)
    conn.execute('''INSERT OR IGNORE INTO library_sources (source_id,name,url,category,first_seen,last_seen)
                    SELECT source_id,source_id,'',MIN(category),MIN(fetched_at),MAX(fetched_at)
                    FROM items GROUP BY source_id''')
    conn.execute('''INSERT OR IGNORE INTO item_sources (item_id,source_id,first_seen,last_seen)
                    SELECT id,source_id,fetched_at,fetched_at FROM items''')


@dataclass
class LibraryPage:
    cards: list
    total: int
    counts: dict
    next_after: int | None
    source_options: list
    origins: dict
    topics: dict


def library_page(conn, cfg, sources, *, view='pending', category='all', source='', query='',
                 order='oldest', after=0, limit=60, filters=None):
    """No age, ranking or per-source cap can silently remove an unread story from this inbox.

    Pagination uses the first stored item id, so marking a page read does not skip the next page.
    """
    names = {r['source_id']: r['name'] for r in conn.execute('SELECT source_id,name FROM library_sources')}
    names.update({s.id: s.name for s in sources})
    rows = conn.execute('''SELECT i.*,s.source_count FROM items i
                           JOIN stories s ON s.id=i.story_id ORDER BY i.id''').fetchall()
    memberships = defaultdict(list)
    for row in conn.execute('''SELECT x.item_id,x.source_id,s.name,s.url,s.category,x.first_seen
                               FROM item_sources x JOIN library_sources s ON s.source_id=x.source_id'''):
        memberships[row['item_id']].append(dict(row))
    options = [dict(r) for r in conn.execute('''SELECT s.source_id,s.name,COUNT(x.item_id) AS items
                FROM library_sources s JOIN item_sources x ON x.source_id=s.source_id
                GROUP BY s.source_id ORDER BY s.name COLLATE NOCASE''')]
    filters = filters or ArticleFilter(query=query)
    classifier = TopicClassifier(topic_rules(conn))
    filtered = []
    origins = {}
    topics = {}
    counts = {'pending': 0, 'read': 0, 'saved': 0}
    for card in make_cards(rows, cfg, names):
        if story_hidden(card):
            continue
        members = [card.lead, *card.others]
        matches = {r['source_id']: r for m in members for r in memberships[m.id]}
        origins[card.lead.id] = list(matches.values())
        topics[card.lead.id] = classifier.classify(card)
        if source and source not in matches and not any(m.row['source_id'] == source for m in members):
            continue
        categories = {m.row['category'] for m in members} | {r['category'] for r in matches.values()}
        if category != 'all' and category not in categories:
            continue
        if not matches_card(card, topics[card.lead.id], filters,
                            source_names=[r['name'] for r in matches.values()]):
            continue
        counts['read' if card.read else 'pending'] += 1
        counts['saved'] += int(card.saved)
        if (view == 'pending' and card.read) or (view == 'read' and not card.read) or (view == 'saved' and not card.saved):
            continue
        key = min(m.id for m in members)
        filtered.append((key, card))
    total = len(filtered)
    descending = order == 'newest'
    filtered.sort(key=lambda pair: pair[0], reverse=descending)
    if after:
        filtered = [(key,c) for key,c in filtered if (key < after if descending else key > after)]
    limit = max(1, min(limit, 100))
    page = filtered[:limit]
    next_after = page[-1][0] if len(filtered) > limit else None
    return LibraryPage([c for _,c in page], total, counts, next_after, options, origins, topics)


def serialize_page(page):
    return {'total': page.total, 'counts': page.counts, 'next_after': page.next_after, 'items': [
        {'id': c.lead.id, 'ids': c.ids, 'title': c.lead.row['title'], 'url': c.lead.row['url'],
         'snippet': c.lead.row['snippet'], 'published': c.lead.row['published'],
         'collected': c.lead.row['fetched_at'], 'read': c.read, 'saved': c.saved, 'rating': card_rating(c),
         'topics': page.topics.get(c.lead.id, []),
         'sources': page.origins.get(c.lead.id, [])} for c in page.cards]}
