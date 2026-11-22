from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from newsnest.config import Source
from newsnest.db import connect, iso, utcnow
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.watches import Prefilter
from tests.test_reels import reader


def add_story(cfg, title, *, source='general', published=None, snippet='', url=None):
    conn = connect()
    src = Source(id=source,type='rss',category='world',name='Medio general',url='https://example.com/rss')
    ingest(conn,src,[Item(source,'world',title,url or 'https://example.com/'+str(abs(hash(title))),
           title, published or utcnow(),snippet=snippet)],Prefilter(cfg),utcnow())
    row = conn.execute('SELECT MAX(id) FROM items').fetchone()[0]
    conn.close()
    return row


def test_content_classification_is_multilabel_not_inherited_from_outlet(reader,cfg):
    item_id = add_story(cfg,'DeepSeek y Qwen renuevan sus modelos para agentes')
    data = reader.get('/reels/library?topic=china').json()
    assert data['total'] == 1 and data['items'][0]['id'] == item_id
    labels = {t['id']:t for t in data['items'][0]['topics']}
    assert {'china','models','harness','ai'} <= labels.keys()
    assert 'DeepSeek' in labels['china']['matched']
    assert data['items'][0]['sources'][0]['category'] == 'world'
    assert reader.get('/reels/library?topic=china&f=tech').json()['total'] == 0
    page = reader.get('/reels?topic=china').text
    assert '¿Por qué estos temas?' in page and 'Coincide con:' in page
    assert reader.get('/reels/library?topic=unclassified').json()['total'] == 1


def test_classifier_ignores_search_expression_and_html_and_handles_ambiguous_names(reader,cfg):
    add_story(cfg,'Estudio sobre animales marinos',snippet='<span class="DeepSeek">Océanos</span>',
              url='https://example.com/article?q=Qwen')
    add_story(cfg,'Kimi Antonelli gana la carrera')
    assert reader.get('/reels/library?topic=china').json()['total'] == 0
    assert reader.get('/reels/library?topic=models').json()['total'] == 0
    assert reader.get('/reels/library?topic=ai').json()['total'] == 0
    add_story(cfg,'v0.99.1',url='https://github.com/openai/codex/releases/tag/v0.99.1')
    assert reader.get('/reels/library?topic=harness').json()['total'] == 1


def test_topics_edit_pause_remove_reclassifies_existing_library_without_mutating_items(reader,cfg):
    item_id = add_story(cfg,'Nueva tecnología de baterías de sodio')
    before = reader.get('/reels/library').json()['counts']
    r = reader.post('/reels/manage/topic',data={'name':'Energía','include':'baterías, sodio','exclude':'litio'})
    assert r.status_code == 200 and 'Tema actualizado' in r.text
    conn = connect()
    from newsnest.topics import topic_rules
    rule = next(t for t in topic_rules(conn) if t['name']=='Energía')
    conn.close()
    path = '/reels/library?topic='+rule['id']
    assert reader.get(path).json()['items'][0]['id'] == item_id
    reader.post('/reels/manage/topic/'+rule['id'],data={'action':'toggle'})
    assert reader.get(path).json()['total'] == 0
    reader.post('/reels/manage/topic/'+rule['id'],data={'action':'toggle'})
    assert reader.get(path).json()['total'] == 1
    reader.post('/reels/manage/topic/'+rule['id'],data={'name':'Energía','include':'baterías','exclude':'sodio'})
    assert reader.get(path).json()['total'] == 0
    reader.post('/reels/manage/topic/'+rule['id'],data={'action':'remove'})
    assert reader.get('/reels/library').json()['counts'] == before
    assert reader.get('/reels/library').json()['total'] == 2
    r=reader.post('/reels/manage/topic',data={'name':'Vacío','include':''})
    assert 'Escribe un nombre' in r.text


def test_combine_topic_date_reaction_saved_source_and_exclusions(reader,cfg):
    wanted = add_story(cfg,'Qwen y DeepSeek mejoran razonamiento',source='wanted')
    add_story(cfg,'Qwen patrocina un torneo de fútbol',source='sports')
    old = add_story(cfg,'DeepSeek publica resultados históricos')
    conn = connect()
    conn.execute('UPDATE items SET published=? WHERE id=?',(iso(utcnow()-timedelta(days=8)),old))
    conn.close()
    reader.post('/reels/reaction/'+str(wanted),data={'value':1})
    reader.post('/api/saved',data={'ids':wanted,'value':1})
    response=reader.get('/reels/library?topic=china&period=24&reaction=liked&bookmarked=1&source=wanted&exclude=futbol')
    assert [i['id'] for i in response.json()['items']] == [wanted]
    assert reader.get('/reels/library?topic=china&exclude=futbol').json()['total'] == 2
    assert reader.get('/reels/library?topic=china&period=168').json()['total'] == 2
    assert reader.get('/reels/library?topic=china&reaction=unrated').json()['total'] == 2
    reader.post('/api/read',data={'ids':wanted,'value':1})
    assert reader.get('/reels/library?view=all&topic=china').json()['total'] == 3
    assert reader.get('/reels/library?view=read&bookmarked=1&topic=china').json()['total'] == 1


def test_advanced_search_quotes_or_exclude_and_accents(reader,cfg):
    add_story(cfg,'Mercado Libre amplía sus operaciones en Chile')
    add_story(cfg,'DeepSeek incorpora razonamiento')
    add_story(cfg,'Qwen mejora generación de imágenes')
    data=reader.get('/reels/library',params={'q':'"Mercado Libre" OR DeepSeek'}).json()
    assert data['total'] == 2
    assert reader.get('/reels/library',params={'q':'Qwen -imagenes'}).json()['total'] == 0
    assert reader.get('/reels/library',params={'q':'"Mercado Libre'}).status_code == 200
    for q in ('OR','Qwen OR','-Qwen'):
        assert reader.get('/reels/library',params={'q':q}).status_code == 200


def test_saved_view_is_local_persistent_and_cannot_store_external_redirect_or_cursor(reader):
    params={'name':'Mi vista','view':'all','topic':'china','exclude':'fútbol','q':'DeepSeek OR Qwen',
            'period':'168','reaction':'liked','source':'theverge','order':'newest','bookmarked':'1',
            'after':'123','next':'https://evil.example/','url':'https://evil.example/','password':'example-private'}
    result=reader.post('/reels/views',data=params)
    assert result.status_code == 200
    data=result.json()
    assert data['url'].startswith('/reels?')
    saved=parse_qs(urlsplit(data['url']).query)
    assert saved['q']==['DeepSeek OR Qwen'] and saved['topic']==['china']
    assert saved['period']==['168'] and saved['reaction']==['liked']
    assert not {'after','next','url','password','name'} & saved.keys()
    from fastapi.testclient import TestClient
    from newsnest.web import app
    with TestClient(app) as reopened:
        assert 'Mi vista' in reopened.get('/reels').text
    assert reader.post('/reels/views',data=params).status_code == 400
    assert reader.post('/reels/views',data={'name':''}).status_code == 400
    reader.post('/reels/views/'+data['id']+'/remove')
    assert 'Mi vista' not in reader.get('/reels').text


def test_advanced_filters_apply_to_actualidad_too(reader,cfg):
    add_story(cfg,'Qwen incorpora nuevas capacidades de razonamiento')
    assert reader.get('/reels?view=latest&topic=china').text.count('class="reel" data-id=') == 1
    assert reader.get('/reels?view=latest&topic=china&exclude=Qwen').text.count('class="reel" data-id=') == 0
    assert reader.get('/reels?view=latest&reaction=liked').text.count('class="reel" data-id=') == 0


def test_date_filter_excludes_undated_future_but_never_deletes_them(reader,cfg):
    conn = connect()
    for value in (None,iso(utcnow()+timedelta(days=1))):
        conn.execute('UPDATE items SET published=?',(value,))
        assert reader.get('/reels/library?period=24').json()['total'] == 0
        assert reader.get('/reels/library').json()['total'] == 1
    conn.close()


@pytest.mark.auth
def test_filter_settings_and_saved_views_require_login(env,monkeypatch):
    monkeypatch.setenv('NEWSNEST_SCHEDULER','off')
    monkeypatch.setenv('NEWSNEST_PASSWORD','qa-password')
    from fastapi.testclient import TestClient
    from newsnest.web import app
    with TestClient(app) as client:
        for path in ('/reels/views','/reels/views/id/remove','/reels/manage/topic','/reels/manage/topic/china'):
            assert client.post(path,data={}).status_code == 401
