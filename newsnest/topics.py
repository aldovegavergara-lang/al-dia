"""Editable multi-label topics, derived from collected content without network or AI calls."""
from __future__ import annotations

import re
from copy import deepcopy
from html import unescape
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from newsnest.db import dumps, get_meta, loads, set_meta, transaction
from newsnest.textmatch import fold

TOPIC_KEY = 'aldia.topic_rules.v1'
DEFAULT_RULES = [
    {'id':'china','name':'Modelos chinos','include':'DeepSeek, Qwen, Kimi, Moonshot, Zhipu, GLM, MiniMax, Hunyuan, ERNIE, Baidu'},
    {'id':'models','name':'Modelos y asistentes','include':'GPT, ChatGPT, Claude, Gemini, Llama, DeepSeek, Qwen, Kimi, GLM, MiniMax, language model, language models, modelo de IA, modelos de IA, LLM, LLMs'},
    {'id':'harness','name':'Agentes y harness','include':'Codex, Claude Code, OpenCode, Hermes Agent, OpenHands, harness, agent, agents, agente, agentes, agentic, MCP, LangChain, LangGraph, tool calling'},
    {'id':'meli','name':'Mercado Libre','include':'Mercado Libre, MercadoLibre, Mercado Pago, MercadoPago, MELI, Mercado Crédito, MercadoCredito'},
    {'id':'ai','name':'Inteligencia artificial','include':'inteligencia artificial, artificial intelligence, OpenAI, Anthropic, DeepMind, Hugging Face, HuggingFace, ChatGPT, Claude, Gemini, LLM, LLMs, DeepSeek, Qwen, Kimi, MiniMax'},
    {'id':'releases','name':'Versiones y funciones','include':'release, releases, changelog, nueva versión, nuevas versiones, new version, new feature, new features, nueva función, nuevas funciones'},
    {'id':'economy','name':'Economía y mercados','include':'economía, economy, inflación, inflation, finanzas, Wall Street, Banco Central, tasa de interés, bolsa, dólar, Nasdaq, fintech'},
    {'id':'chile','name':'Chile','include':'Chile, chileno, chilena, chilenos, chilenas, Santiago, La Moneda'},
]
for _rule in DEFAULT_RULES:
    _rule.update(exclude='', enabled=True)
    if _rule['id'] in ('china','models','ai'):
        _rule['exclude'] = 'Kimi Antonelli, Claude Monet, Claude Debussy'


def phrases(raw):
    return list(dict.fromkeys(s.strip() for s in re.split(r'[,\n]', raw) if s.strip()))


def topic_rules(conn):
    stored = get_meta(conn, TOPIC_KEY)
    return loads(stored) if stored is not None else deepcopy(DEFAULT_RULES)


def edit_topic(conn, *, topic_id='', action='save', name='', include='', exclude=''):
    if action not in ('save','toggle','remove'):
        raise ValueError('Acción de tema desconocida.')
    name, include, exclude = name.strip(), include.strip(), exclude.strip()
    if action == 'save':
        if not name or len(name) > 80 or not phrases(include):
            raise ValueError('Escribe un nombre y al menos una palabra o frase para el tema.')
        if any(len(raw) > 2500 or len(phrases(raw)) > 40 or any(len(p) > 100 for p in phrases(raw))
               for raw in (include, exclude)):
            raise ValueError('Usa hasta 40 frases de 100 caracteres por campo.')
    with transaction(conn):
        rules = topic_rules(conn)
        rule = next((r for r in rules if r['id'] == topic_id), None)
        if topic_id and not rule:
            raise ValueError('Este tema ya no existe.')
        if action == 'remove':
            rules.remove(rule)
        elif action == 'toggle':
            rule['enabled'] = not rule['enabled']
        else:
            if any(fold(r['name']) == fold(name) and r is not rule for r in rules):
                raise ValueError('Ya hay un tema con ese nombre.')
            if rule is None:
                if len(rules) >= 24:
                    raise ValueError('Puedes mantener hasta 24 temas.')
                rule = {'id': uuid4().hex[:16], 'enabled':True}
                rules.append(rule)
            rule.update(name=name, include=include, exclude=exclude)
        set_meta(conn, TOPIC_KEY, dumps(rules))


def plain(text):
    return ' '.join(unescape(re.sub(r'<[^>]*>', ' ', text or '')).split())


def content_text(card):
    return fold(' '.join(plain(str(m.row[k] or '')) for m in [card.lead,*card.others]
                         for k in ('title','snippet','publisher')))


class TopicClassifier:
    def __init__(self, rules):
        self.rules = []
        for r in rules:
            if not r['enabled']:
                continue
            def compile_terms(raw):
                return [(p, fold(p), re.compile(r'(?<!\w)' + re.escape(fold(p)) + r'(?!\w)')) for p in phrases(raw)]
            self.rules.append((r, compile_terms(r['include']), compile_terms(r['exclude'])))

    def classify(self, card):
        text = content_text(card)
        # Release notes often contain only a version number. Official URL paths add useful context;
        # query strings (including search expressions) never become classification evidence.
        urls = [urlsplit(m.row['url']) for m in [card.lead,*card.others]]
        text += ' ' + fold(' '.join(unquote((u.hostname or '') + u.path) for u in urls))
        result = []
        for rule, includes, excludes in self.rules:
            # Most terms are absent. The literal check avoids scanning the whole article with
            # a lookbehind regex for each absent term, while preserving exact word boundaries.
            matched = [p for p, needle, pattern in includes if needle in text and pattern.search(text)]
            if matched and not any(needle in text and pattern.search(text) for _, needle, pattern in excludes):
                result.append({'id':rule['id'], 'name':rule['name'], 'matched':matched})
        return result
