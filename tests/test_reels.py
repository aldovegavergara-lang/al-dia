from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from newsnest.config import Source, load_config
from newsnest.db import connect, iso, migrate, utcnow
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.watches import Prefilter


@pytest.fixture
def reader(env, cfg, monkeypatch):
    monkeypatch.setenv('NEWSNEST_SCHEDULER', 'off')
    conn = connect()
    migrate(conn)
    source = Source(id='theverge', type='rss', category='tech', name='The Verge', url='https://www.theverge.com/rss/index.xml')
    ingest(conn, source, [Item(source.id, 'tech', 'Una noticia realista de prueba',
                              'https://example.com/story', 'reels-test', utcnow(),
                              snippet='<b>Descripción</b> del medio.')], Prefilter(cfg), utcnow())
    conn.close()
    from newsnest.web import app
    with TestClient(app) as client:
        yield client


def test_reader_and_saved_story_share_persisted_bookmarks(reader):
    page = reader.get('/reels')
    assert page.status_code == 200 and 'Una noticia realista de prueba' in page.text
    assert 'Extracto de la fuente' in page.text and 'RESUMEN CON IA' not in page.text
    assert 'data-id="1"' in page.text
    saved = reader.post('/api/saved', data={'ids': '1', 'value': '1'}, headers={'Accept': 'application/json'})
    assert saved.status_code == 200
    assert 'Una noticia realista de prueba' in reader.get('/reels?saved=1').text
    reader.post('/api/saved', data={'ids': '1', 'value': '0'}, headers={'Accept': 'application/json'})
    assert 'Una noticia realista de prueba' not in reader.get('/reels?saved=1').text


def test_search_is_a_persistent_polled_feed_and_can_pause_remove(reader, env):
    query = '"Proveedor & producto" site:example.com'
    response = reader.post('/reels/manage/source', data={
        'name': 'Mi proveedor', 'value': query, 'kind': 'search', 'category': 'tech'})
    assert response.status_code == 200 and 'Mi proveedor' in response.text
    cfg = load_config(env.config).config
    feed = next(f for f in cfg.feeds if f.name == 'Mi proveedor')
    assert parse_qs(urlsplit(feed.url).query)['q'] == [query]
    assert feed.enabled
    duplicate = reader.post('/reels/manage/source', data={
        'name': 'Otra vez', 'value': query, 'kind': 'search', 'category': 'tech'})
    assert 'already a feed' in duplicate.text
    reader.post(f'/reels/manage/source/{feed.id}', data={'action': 'toggle'})
    assert not next(f for f in load_config(env.config).config.feeds if f.id == feed.id).enabled
    reader.post(f'/reels/manage/source/{feed.id}', data={'action': 'remove'})
    assert all(f.id != feed.id for f in load_config(env.config).config.feeds)


def test_preferences_change_collector_frequency_without_restart(reader, env):
    response = reader.post('/reels/manage/preferences', data={'minutes': '60', 'interests': '## Want\n- Mi proveedor'})
    assert response.status_code == 200
    cfg = load_config(env.config).config
    assert all(cfg.poll_minutes(s.key) == 60 for s in cfg.all_sections)
    assert 'Mi proveedor' in (env.config / 'interests.md').read_text()
    from datetime import timedelta
    from newsnest.runner import is_due
    from newsnest.db import iso
    source = load_config(env.config).sources[0]
    state = {'last_run': iso(utcnow() - timedelta(minutes=31)), 'blocked_until': None}
    assert not is_due(source, state, cfg, utcnow())


def test_invalid_source_does_not_change_configuration(reader, env):
    before = (env.config / 'config.yaml').read_text()
    r = reader.post('/reels/manage/source', data={
        'name': 'Mala fuente', 'value': 'javascript:alert(1)', 'kind': 'rss', 'category': 'tech'})
    assert 'Usa la dirección HTTPS' in r.text
    assert (env.config / 'config.yaml').read_text() == before


def test_new_article_indicator_and_filter(reader):
    assert reader.get('/reels/state?since=0').json()['new_articles'] == 1
    assert reader.get('/reels/state?since=1').json()['new_articles'] == 0
    assert 'Una noticia realista de prueba' not in reader.get('/reels?source=missing').text


def test_reload_includes_news_collected_since_opening(reader, cfg):
    assert reader.get('/reels').status_code == 200
    conn = connect()
    source = Source(id='theverge', type='rss', category='tech', name='The Verge', url='https://www.theverge.com/rss/index.xml')
    ingest(conn, source, [Item(source.id, 'tech', 'Nueva noticia después de abrir el lector',
                              'https://example.com/new', 'new-story', utcnow())], Prefilter(cfg), utcnow())
    conn.close()
    assert 'Nueva noticia después de abrir el lector' in reader.get('/reels').text


def test_search_card_keeps_original_publisher():
    from newsnest.fetchers.rss import entry_to_item
    source = Source(id='search-vendor', type='rss', category='tech', name='Mi proveedor',
                    url='https://news.google.com/rss/search?q=vendor')
    item = entry_to_item({'title': 'Nueva versión', 'link': 'https://example.com/a',
                          'source': {'title': 'El medio original'}}, source)
    assert item.publisher == 'El medio original'


def test_indexed_social_source_is_labelled_as_partial():
    from newsnest.reels import source_kind, search_url
    assert source_kind(search_url('site:x.com Qwen')) == 'X · indexación pública parcial'
    assert source_kind('https://medium.com/feed/tag/ai-agents') == 'Medium · RSS'
    assert source_kind('https://github.com/openai/codex/releases.atom') == 'GitHub · versiones estables'


def test_old_publication_does_not_become_news_when_discovered_today(reader):
    from datetime import timedelta
    from newsnest.db import iso
    conn = connect()
    conn.execute('UPDATE items SET published = ? WHERE id = 1', (iso(utcnow() - timedelta(hours=48)),))
    conn.commit()
    conn.close()
    assert 'Una noticia realista de prueba' not in reader.get('/reels?view=latest').text
    reader.post('/reels/reaction/1',data={'value':1})
    assert 'Una noticia realista de prueba' not in reader.get('/reels?view=latest').text
    assert reader.get('/reels/state?since=0&view=latest').json()['new_articles'] == 0
    assert 'Una noticia realista de prueba' in reader.get('/reels?view=latest&hours=72').text
    assert 'Una noticia realista de prueba' in reader.get('/reels').text
    reader.post('/api/saved', data={'ids': '1', 'value': '1'}, headers={'Accept': 'application/json'})
    assert 'Una noticia realista de prueba' in reader.get('/reels?saved=1').text


def test_missing_or_future_publication_is_not_shown_as_current(reader):
    from datetime import timedelta
    from newsnest.db import iso
    conn = connect()
    conn.execute('UPDATE items SET published = NULL WHERE id = 1')
    conn.commit()
    assert 'Una noticia realista de prueba' not in reader.get('/reels?view=latest').text
    assert reader.get('/reels/state?since=0&view=latest').json()['new_articles'] == 0
    reader.post('/api/saved', data={'ids': '1', 'value': '1'}, headers={'Accept': 'application/json'})
    assert 'Fecha de publicación no informada' in reader.get('/reels?saved=1').text
    conn.execute('UPDATE items SET published = ? WHERE id = 1', (iso(utcnow() + timedelta(days=1)),))
    conn.commit()
    conn.close()
    assert 'Una noticia realista de prueba' not in reader.get('/reels?view=latest').text
    assert reader.get('/reels/state?since=0&view=latest').json()['new_articles'] == 0


def test_unread_history_survives_age_limits_and_read_state_can_be_undone(reader):
    from datetime import timedelta
    from newsnest.db import iso
    conn = connect()
    old = iso(utcnow() - timedelta(days=30))
    conn.execute('UPDATE items SET published=?,fetched_at=? WHERE id=1', (old,old))
    conn.close()
    assert reader.get('/reels/library').json()['counts']['pending'] == 1
    assert 'Una noticia realista de prueba' in reader.get('/reels').text
    assert 'Una noticia realista de prueba' not in reader.get('/reels?view=latest&hours=72').text
    reader.post('/api/read',data={'ids':'1','value':'1'},headers={'Accept':'application/json'})
    assert reader.get('/reels/library').json()['total'] == 0
    assert reader.get('/reels/library?view=read').json()['items'][0]['read'] is True
    from newsnest.web import app
    with TestClient(app) as restarted:
        assert restarted.get('/reels/library?view=read').json()['total'] == 1
    reader.post('/api/read',data={'ids':'1','value':'0'},headers={'Accept':'application/json'})
    assert reader.get('/reels/library').json()['items'][0]['read'] is False


def test_one_url_keeps_multiple_search_origins_and_does_not_reset_reading(reader,cfg):
    conn = connect()
    other = Source(id='former-query',type='rss',category='news',name='Búsqueda anterior de Qwen',
                   url='https://example.com/rss?q=qwen')
    assert ingest(conn,other,[Item(other.id,'news','Una noticia realista de prueba',
                 'https://example.com/story','another-guid',utcnow())],Prefilter(cfg),utcnow()) == []
    assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] == 1
    conn.close()
    data = reader.get('/reels/library?source=former-query&f=news&q=Qwen').json()
    assert data['total'] == 1
    assert {s['source_id'] for s in data['items'][0]['sources']} == {'former-query','theverge'}
    assert 'Búsqueda anterior de Qwen' in reader.get('/reels').text
    reader.post('/api/read',data={'ids':'1','value':'1'},headers={'Accept':'application/json'})
    conn = connect()
    ingest(conn,other,[Item(other.id,'news','Una noticia realista de prueba',
           'https://example.com/story','again',utcnow())],Prefilter(cfg),utcnow())
    conn.close()
    assert reader.get('/reels/library?source=former-query').json()['total'] == 0


def test_pending_pagination_does_not_skip_after_reading_a_page(reader,cfg):
    conn = connect()
    source = Source(id='theverge',type='rss',category='tech',name='The Verge',url='https://example.com/rss')
    for n in range(4):
        ingest(conn,source,[Item(source.id,'tech',f'Story{n} Topic{n} Fresh{n}',
               f'https://example.com/pagination/{n}',str(n),utcnow())],Prefilter(cfg),utcnow())
    conn.close()
    first = reader.get('/reels/library?limit=2').json()
    assert first['total'] == 5 and first['next_after'] is not None
    read_ids = ','.join(it['ids'] for it in first['items'])
    reader.post('/api/read',data={'ids':read_ids,'value':'1'},headers={'Accept':'application/json'})
    second = reader.get(f'/reels/library?limit=2&after={first["next_after"]}').json()
    assert [i['id'] for i in second['items']] == [3,4]
    assert second['counts']['pending'] == 3


def test_reactions_are_reversible_persistent_and_separate_from_reading(reader, cfg):
    conn = connect()
    source = Source(id='other-outlet',type='rss',category='tech',name='Otro medio',url='https://example.com/rss')
    ingest(conn,source,[Item(source.id,'tech','Una noticia realista de prueba',
           'https://another.example/story','same-story',utcnow())],Prefilter(cfg),utcnow())
    conn.close()
    before = reader.get('/reels/library').json()
    assert before['total'] == 1
    for value in (1, -1, 0):
        response = reader.post('/reels/reaction/1',data={'value':value})
        assert response.status_code == 200
        conn = connect()
        rows = conn.execute('SELECT rating,rated_at,read_at,saved_at,hidden_at FROM items').fetchall()
        assert len(rows) == 2
        assert all(r['rating'] == (value or None) for r in rows)
        assert all(bool(r['rated_at']) == bool(value) for r in rows)
        assert all(r['read_at'] is None and r['saved_at'] is None and r['hidden_at'] is None for r in rows)
        from newsnest.reactions import load_tastes
        assert len(load_tastes(conn,cfg)) == (1 if value else 0)
        conn.close()
        data = reader.get('/reels/library').json()
        assert data['items'][0]['rating'] == value
        assert data['counts'] == before['counts']
        from newsnest.web import app
        with TestClient(app) as reopened:
            assert reopened.get('/reels/library').json()['items'][0]['rating'] == value
    assert reader.post('/reels/reaction/1',data={'value':2}).status_code == 400
    assert reader.post('/reels/reaction/99999',data={'value':1}).status_code == 404


def test_actualidad_uses_reactions_but_keeps_dates_and_library_order(reader,cfg):
    conn = connect()
    conn.execute("UPDATE items SET title='DeepSeek Qwen modelos chinos lanzamiento',snippet='',published=? WHERE id=1",
                 (iso(utcnow()),))
    source = Source(id='fresh-news',type='rss',category='tech',name='Noticias',url='https://example.com/rss')
    for title,url in [('Qwen mejora razonamiento y modelos chinos','related'),
                      ('Astronautas preparan telescopio orbital espacial','unrelated')]:
        ingest(conn,source,[Item(source.id,'tech',title,f'https://example.com/{url}',url,utcnow())],Prefilter(cfg),utcnow())
    conn.close()
    # Keep selection and base ranks equal: this verifies the Actualidad route applies personal tastes.
    from types import SimpleNamespace
    from newsnest.digest import make_cards
    import newsnest.web as ui
    with pytest.MonkeyPatch.context() as patch:
        conn = connect()
        cards = make_cards(conn.execute('SELECT * FROM items ORDER BY id').fetchall(),cfg,{})
        conn.close()
        patch.setattr(ui.feedsession,'open_session',lambda *a,**kw:(None,object()))
        patch.setattr('newsnest.reels.view_feed',lambda *a:SimpleNamespace(items=[SimpleNamespace(card=c,rank=1) for c in cards]))
        reader.post('/reels/reaction/1',data={'value':-1})
        # Refresh rows: reaction state in the history is live; candidate selection holds still.
        page = reader.get('/reels?view=latest').text
        assert page.index('data-id="3"') < page.index('data-id="2"')
        reader.post('/reels/reaction/1',data={'value':1})
        page = reader.get('/reels?view=latest').text
        assert page.index('data-id="2"') < page.index('data-id="3"')
        reader.post('/reels/reaction/1',data={'value':0})
        page = reader.get('/reels?view=latest').text
        assert page.index('data-id="2"') < page.index('data-id="3"')
    data = reader.get('/reels/library').json()
    assert [r['id'] for r in data['items']] == [1,2,3]
    assert data['counts']['pending'] == 3


def test_reactions_have_no_effect_without_votes_and_prioritize_similar_titles(conn,cfg):
    from newsnest.digest import make_cards
    from newsnest.reactions import load_tastes, preference_multiplier
    source = Source(id='theverge',type='rss',category='tech',name='The Verge',url='https://example.com/rss')
    for n,title in enumerate(['Qwen mejora modelos chinos razonamiento','Qwen presenta modelos chinos avanzados',
                              'Astronautas preparan telescopio orbital espacial']):
        ingest(conn,source,[Item(source.id,'tech',title,f'https://example.com/taste/{n}',str(n),utcnow())],Prefilter(cfg),utcnow())
    cards = make_cards(conn.execute('SELECT * FROM items ORDER BY id').fetchall(),cfg,{})
    assert len(cards) == 3
    assert all(preference_multiplier(c,load_tastes(conn,cfg)) == 1 for c in cards)
    for vote in (-1,1):
        from newsnest.db import iso
        conn.execute('UPDATE items SET rating=?,rated_at=? WHERE id=1',(vote,iso(utcnow())))
        tastes = load_tastes(conn,cfg)
        related,unrelated = [preference_multiplier(c,tastes) for c in cards[1:]]
        assert (related > unrelated) if vote == 1 else (related < unrelated)


@pytest.mark.auth
def test_new_endpoints_keep_upstream_login_protection(env, monkeypatch):
    monkeypatch.setenv('NEWSNEST_SCHEDULER', 'off')
    monkeypatch.setenv('NEWSNEST_PASSWORD', 'test-password')
    from newsnest.web import app
    with TestClient(app) as client:
        assert client.get('/reels', follow_redirects=False).status_code == 303
        assert client.post('/reels/refresh').status_code == 401
        assert client.post('/reels/manage/source', data={}).status_code == 401
        assert client.post('/reels/reaction/1', data={'value':1}).status_code == 401
