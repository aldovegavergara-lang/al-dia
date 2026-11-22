"""Al día: a Spanish, full-screen news reader on top of NewsNest's collector.

Local adaptation, November 2026. Upstream code and AGPL license are preserved.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from newsnest.bootstrap import config_dir, env
from newsnest.config import LoadedConfig, load_config
from newsnest.config_edit import ConfigEditError, edit_config, section_list
from newsnest.db import get_meta, iso, parse_iso, transaction, utcnow
from newsnest.discovery import named_sources
from newsnest.feed import view_feed
from newsnest.profile import read_profile, write_profile
from newsnest.library import library_page, serialize_page
from newsnest.reactions import card_rating, load_tastes, preference_multiplier
from newsnest.reader_filters import ArticleFilter, matches_card, remove_view, save_view, saved_views, view_url
from newsnest.topics import TopicClassifier, edit_topic, topic_rules

_settings_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None


def search_url(query: str) -> str:
    return 'https://news.google.com/rss/search?' + urlencode({
        'q': query, 'hl': 'es-419', 'gl': 'CL', 'ceid': 'CL:es-419'})


def source_kind(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ''
    query = parse_qs(parts.query).get('q', [''])[0]
    if host == 'news.google.com':
        if 'site:x.com' in query or 'site:twitter.com' in query:
            return 'X · indexación pública parcial'
        if 'site:reddit.com' in query:
            return 'Reddit · búsqueda indexada'
        return 'Búsqueda de noticias'
    if host == 'github.com' and parts.path.endswith('/releases.atom'):
        return 'GitHub · versiones estables'
    if host == 'medium.com' or host.endswith('.medium.com'):
        return 'Medium · RSS'
    if host == 'reddit.com' or host.endswith('.reddit.com'):
        return 'Reddit · RSS'
    return 'RSS / Atom'


def published_recently(card, hours: int, now=None) -> bool:
    """Discovery time must never make an old or undated story look current."""
    now = now or utcnow()
    published = parse_iso(card.lead.row['published'])
    return bool(published and now - timedelta(hours=hours) <= published <= now + timedelta(minutes=5))


def publication_age(value, now=None):
    """Elapsed time, never discovery time or a fabricated age for future publications."""
    try:
        stamp = parse_iso(value)
    except (ValueError, TypeError):
        stamp = None
    if not stamp:
        return 'Fecha no informada'
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = int(((now or utcnow()) - stamp).total_seconds())
    if seconds < -300:
        return 'Fecha por confirmar'
    if seconds < 60:
        return 'Hace un momento'
    minutes = seconds // 60
    if minutes < 60:
        return f'Hace {minutes} minuto' + ('s' if minutes != 1 else '')
    hours = minutes // 60
    if hours < 24:
        return f'Hace {hours} hora' + ('s' if hours != 1 else '')
    days, hours = divmod(hours, 24)
    text = f'Hace {days} día' + ('s' if days != 1 else '')
    if hours:
        text += f' y {hours} hora' + ('s' if hours != 1 else '')
    return text


def register(app, ui):
    """Reuse upstream auth, data, ranking and bookmarks instead of duplicating them."""
    @app.get('/reels', response_class=HTMLResponse)
    def reader(request: Request, f: str = 'all', source: str = '', saved: int = 0, hours: int = 24,
               view: str = 'pending', q: str = '', order: str = '', after: int = 0,
               topic: str = 'all', period: int = 0, reaction: str = 'all', exclude: str = '', bookmarked: int = 0,
               conn=Depends(ui.get_conn), loaded: LoadedConfig = Depends(ui.get_loaded)):
        cfg = ui._require_cfg(loaded)
        sources = named_sources(conn, cfg, loaded.sources)
        view = 'saved' if saved else view if view in ('pending', 'latest', 'read', 'saved','all') else 'pending'
        order = order if order in ('oldest','newest') else 'oldest' if view == 'pending' else 'newest'
        q = q.strip()[:200]
        hours = hours if hours in (24, 72) else 24
        filters = ArticleFilter(topic=topic, period=period, reaction=reaction, exclude=exclude,
                                bookmarked=bool(bookmarked), query=q)
        rules = topic_rules(conn)
        classifier = TopicClassifier(rules)
        library = library_page(conn, cfg, sources, view='pending' if view == 'latest' else view,
                               category=f, source=source, query=q, order=order, after=max(0,after),filters=filters)
        cards, next_after, total = library.cards, library.next_after, library.total
        token = None
        if view == 'latest':
            token, ranking = ui.feedsession.open_session(
                conn, cfg, sources, request.cookies.get(ui.feedsession.COOKIE), fresh=True)
            feed = view_feed(ranking, cfg, f)
            now = utcnow()
            cards = [it.card for it in feed.items if published_recently(it.card, hours, now)]
            if source:
                matched = {r[0] for r in conn.execute('SELECT item_id FROM item_sources WHERE source_id=?', (source,))}
                cards = [c for c in cards if any(m.id in matched for m in [c.lead,*c.others])]
            for c in cards:
                library.topics[c.lead.id] = classifier.classify(c)
            cards = [c for c in cards if matches_card(c, library.topics[c.lead.id],filters,
                     source_names=[r['name'] for r in library.origins.get(c.lead.id,[])],now=now)]
            tastes = load_tastes(conn, cfg)
            ranks = {it.card.lead.id: it.rank for it in feed.items}
            cards.sort(key=lambda c: ranks[c.lead.id] * preference_multiplier(c, tastes), reverse=True)
            next_after, total = None, len(cards)

        def reader_url(**changes):
            params = {'view':view,'f':f,'source':source,'q':q,'hours':hours,'order':order,
                      'topic':filters.topic,'period':filters.period,'reaction':filters.reaction,
                      'exclude':filters.exclude,'bookmarked':int(filters.bookmarked)}
            params.update(changes)
            return '/reels?' + urlencode({k:v for k,v in params.items() if v not in ('',None,0)})

        version = conn.execute('SELECT COALESCE(MAX(id), 0) FROM items').fetchone()[0]
        response = ui.render(request, 'reels.html', conn, loaded, cards=cards,
                             selected=f, source=source, saved=view=='saved', version_id=version, hours=hours,
                             view=view, query=q, order=order, counts=library.counts,
                             library_sources=library.source_options, origins=library.origins,
                             next_after=next_after, total=total, reader_url=reader_url,
                             card_rating=card_rating, publication_age=publication_age,
                             article_filters=filters,topic_rules=rules,
                             card_topics=library.topics,saved_views=saved_views(conn),view_url=view_url,
                             has_filters=bool(filters.active or f!='all' or source))
        if token:
            ui._set_feed_cookie(request, response, token)
        return response

    @app.post('/reels/reaction/{item_id}')
    def react(item_id: int, value: int = Form(...), conn=Depends(ui.get_conn)):
        if value not in (-1, 0, 1):
            raise HTTPException(400, 'Elige me interesa, no me interesa o quitar reacción.')
        with transaction(conn):
            if not conn.execute('SELECT 1 FROM items WHERE id=?', (item_id,)).fetchone():
                raise HTTPException(404, 'La noticia ya no existe.')
            ids = ui._card_items(conn, item_id)
            marks = ','.join('?' * len(ids))
            conn.execute(f'UPDATE items SET rating=?,rated_at=? WHERE id IN ({marks})',
                         (value or None, iso(utcnow()) if value else None, *ids))
        return {'id': item_id, 'rating': value, 'ids': ids}

    @app.get('/reels/library')
    def read_library(view: str = 'pending', f: str = 'all', source: str = '', q: str = '',
                     order: str = 'oldest', after: int = 0, limit: int = 30,
                     topic: str = 'all', period: int = 0, reaction: str = 'all', exclude: str = '', bookmarked: int = 0,
                     conn=Depends(ui.get_conn), loaded: LoadedConfig = Depends(ui.get_loaded)):
        cfg = ui._require_cfg(loaded)
        if view not in ('pending','read','saved','all'):
            raise HTTPException(400, 'Estado de biblioteca desconocido.')
        result = library_page(conn,cfg,named_sources(conn,cfg,loaded.sources),view=view,category=f,
                              source=source,query=q.strip()[:200],order=order,after=max(0,after),limit=limit,
                              filters=ArticleFilter(topic=topic,period=period,reaction=reaction,exclude=exclude,
                                                    bookmarked=bool(bookmarked),query=q))
        return serialize_page(result)

    @app.post('/reels/views')
    async def store_view(request: Request, conn=Depends(ui.get_conn)):
        form = await request.form()
        try:
            view = save_view(conn,str(form.get('name','')),form)
        except ValueError as e:
            raise HTTPException(400,str(e))
        return {'id':view['id'],'url':view_url(view)}

    @app.post('/reels/views/{view_id}/remove')
    def delete_view(view_id: str, conn=Depends(ui.get_conn)):
        remove_view(conn,view_id)
        return back(done='Vista quitada. Tus artículos siguen en la biblioteca.')

    @app.post('/reels/manage/topic')
    @app.post('/reels/manage/topic/{topic_id}')
    def update_topic(topic_id: str = '', action: str = Form('save'),name: str = Form(''),
                     include: str = Form(''), exclude: str = Form(''),conn=Depends(ui.get_conn)):
        try:
            edit_topic(conn,topic_id=topic_id,action=action,name=name,include=include,exclude=exclude)
        except ValueError as e:
            return back(str(e))
        return back(done='Tema actualizado. Se aplica también a las noticias ya recopiladas.')

    @app.get('/reels/state')
    def state(since: int = 0, hours: int = 24, view: str = 'pending', conn=Depends(ui.get_conn)):
        hours = hours if hours in (24, 72) else 24
        now = utcnow()
        if view == 'latest':
            row = conn.execute('SELECT COUNT(*) FROM items WHERE id > ? AND published >= ? AND published <= ?',
                               (max(0, since), iso(now - timedelta(hours=hours)),
                                iso(now + timedelta(minutes=5)))).fetchone()
        else:
            row = conn.execute('''SELECT COUNT(*) FROM items WHERE id > ? AND read_at IS NULL
                                  AND hidden_at IS NULL AND COALESCE(prefilter,'') != 'muted' ''',
                               (max(0, since),)).fetchone()
        return {'new_articles': row[0], 'last_tick': get_meta(conn, 'last_tick'),
                'running': bool(_refresh_thread and _refresh_thread.is_alive())}

    @app.post('/reels/refresh')
    def refresh():
        global _refresh_thread
        from newsnest.runner import tick
        with _settings_lock:
            if not (_refresh_thread and _refresh_thread.is_alive()):
                ai_enabled = (env('AI_ENABLED', 'on') or 'on').lower() not in ('off', '0', 'false')
                _refresh_thread = threading.Thread(target=tick, kwargs={'force': True, 'ai': ai_enabled}, daemon=True)
                _refresh_thread.start()
        return {'started': True}

    @app.get('/reels/manage', response_class=HTMLResponse)
    def manage(request: Request, conn=Depends(ui.get_conn),
               loaded: LoadedConfig = Depends(ui.get_loaded)):
        ui._require_cfg(loaded)
        return ui.render(request, 'reels_manage.html', conn, loaded,
                         interests=read_profile('interests.md'),
                         source_kind=source_kind, topic_rules=topic_rules(conn),
                         saved_views=saved_views(conn), view_url=view_url,
                         error=request.query_params.get('error'),
                         done=request.query_params.get('done'))

    @app.post('/reels/manage/source')
    def add_source(name: str = Form(...), kind: str = Form('search'),
                   value: str = Form(...), category: str = Form('tech')):
        name, value = name.strip(), value.strip()
        cfg = load_config(config_dir()).config
        if not cfg:
            raise HTTPException(503, 'La configuración contiene errores.')
        if not name or len(name) > 100 or not value or len(value) > 2000:
            return back('Escribe un nombre y una búsqueda o dirección válidos.')
        if category not in {s.key for s in cfg.all_sections}:
            return back('Elige una sección válida.')
        if kind not in ('search', 'rss'):
            return back('Tipo de fuente desconocido.')
        url = search_url(value) if kind == 'search' else value
        if kind == 'rss' and (urlsplit(url).scheme != 'https' or not urlsplit(url).hostname
                              or urlsplit(url).username or urlsplit(url).password):
            return back('Usa la dirección HTTPS del RSS o Atom de la fuente.')

        def change(data):
            feeds = section_list(data, 'feeds')
            ui._refuse_duplicate(feeds, url)
            fid = ('search-' if kind == 'search' else 'rss-') + hashlib.sha256(url.encode()).hexdigest()[:12]
            feeds.append({'id': fid, 'name': name, 'url': url, 'category': category,
                          'daily_cap': 20})
        try:
            with _settings_lock:
                edit_config(change)
        except (ConfigEditError, ValueError) as e:
            return back(str(e)[:300])
        return back(done='Fuente añadida. El recolector la consultará en su próxima revisión.')

    @app.post('/reels/manage/source/{source_id}')
    def edit_source(source_id: str, action: str = Form(...)):
        if action not in ('toggle', 'remove'):
            raise HTTPException(400)

        def change(data):
            feeds = section_list(data, 'feeds')
            found = next((f for f in feeds if f['id'] == source_id), None)
            if found is None:
                raise ConfigEditError('La fuente ya no existe.')
            if action == 'remove':
                feeds.remove(found)
            else:
                found['enabled'] = not found.get('enabled', True)
        try:
            with _settings_lock:
                edit_config(change)
        except ConfigEditError as e:
            return back(str(e))
        return back(done='Fuente actualizada.')

    @app.post('/reels/manage/preferences')
    def preferences(interests: str = Form(''), minutes: int = Form(30)):
        if minutes not in (15, 30, 60, 120):
            return back('Elige una frecuencia de 15, 30, 60 o 120 minutos.')
        if len(interests) > 15000:
            return back('La descripción de tus intereses es demasiado larga.')

        def change(data):
            data.setdefault('schedules', {})
            for key in ('news_minutes', 'world_minutes', 'tech_minutes'):
                data['schedules'][key] = minutes
            for section in data.get('sections') or []:
                section['poll_minutes'] = minutes
        try:
            with _settings_lock:
                edit_config(change)
                write_profile('interests.md', interests.strip())
        except ConfigEditError as e:
            return back(str(e)[:300])
        return back(done='Preferencias guardadas. Los cambios se aplican sin reiniciar.')


def back(error: str = '', done: str = ''):
    return RedirectResponse('/reels/manage?' + urlencode({'error': error} if error else {'done': done}),
                            status_code=303)
