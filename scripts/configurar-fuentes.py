"""Apply the optional AI, agents, Chinese models and Mercado Libre source preset.

No API keys or account access. Reddit remains paused until its public RSS responds.
An existing feed with the same ID or URL is retained, including manual settings.
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SUPPORT = Path.home() / 'Library' / 'Application Support' / 'AlDia'
os.environ.setdefault('NEWSNEST_CONFIG_DIR', str(SUPPORT / 'config'))
os.environ.setdefault('NEWSNEST_DATA_DIR', str(SUPPORT / 'data'))

from newsnest.config_edit import edit_config, section_list
from newsnest.profile import write_profile


def news(query, spanish=False):
    return 'https://news.google.com/rss/search?' + urlencode({
        'q': query, 'hl': 'es-419' if spanish else 'en-US',
        'gl': 'CL' if spanish else 'US', 'ceid': 'CL:es-419' if spanish else 'US:en'})


FEEDS = [
    ('openai-news', 'OpenAI · anuncios oficiales', 'https://openai.com/news/rss.xml', 'ai', 5),
    ('huggingface-blog', 'Hugging Face · modelos y comunidad', 'https://huggingface.co/blog/feed.xml', 'ai', 8),
    ('the-decoder', 'The Decoder · actualidad de IA', 'https://the-decoder.com/feed/', 'ai', 5),
    ('medium-towards-ai', 'Medium · Towards AI', 'https://medium.com/feed/towards-artificial-intelligence', 'ai', 4),
    ('medium-llms', 'Medium · modelos de lenguaje', 'https://medium.com/feed/tag/large-language-models', 'ai', 4),
    ('medium-agents', 'Medium · agentes de IA', 'https://medium.com/feed/tag/ai-agents', 'harness', 4),
    ('codex-releases', 'Codex · novedades oficiales', 'https://github.com/openai/codex/releases.atom', 'harness', 3),
    ('claude-code-releases', 'Claude Code · novedades oficiales', 'https://github.com/anthropics/claude-code/releases.atom', 'harness', 3),
    ('opencode-releases', 'OpenCode · novedades oficiales', 'https://github.com/anomalyco/opencode/releases.atom', 'harness', 3),
    ('hermes-releases', 'Hermes Agent · novedades oficiales', 'https://github.com/NousResearch/hermes-agent/releases.atom', 'harness', 3),
    ('search-ai-models', 'Nuevos modelos · búsqueda', news('("AI model" OR "large language model") (launch OR release OR benchmark) when:7d'), 'ai', 6),
    ('search-chinese-models', 'DeepSeek, Qwen, Kimi, GLM y MiniMax', news('(DeepSeek OR Qwen OR Kimi OR "Moonshot AI" OR "Zhipu AI" OR MiniMax OR GLM) (model OR LLM) when:7d'), 'china', 8),
    ('search-harness', 'Agentes y harness · novedades', news('(Codex OR "Claude Code" OR OpenCode OR "Hermes Agent" OR "agent harness") (release OR feature OR update) when:7d'), 'harness', 6),
    ('search-mercadolibre', 'Mercado Libre y Mercado Pago', news('("Mercado Libre" OR MercadoLibre OR "Mercado Pago" OR MELI) -fútbol -futbol -Palestino -"Liga de Primera" -"Liga Profesional" when:7d', True), 'providers', 10),
    ('x-ai-indexed', 'X · modelos y agentes (cobertura parcial)', news('site:x.com (DeepSeek OR Qwen OR Kimi OR MiniMax OR GLM OR "Claude Code" OR OpenCode OR Codex) (AI OR LLM OR model OR coding OR agent) -Antonelli -F1 when:7d'), 'ai', 5),
    ('reddit-localllama', 'Reddit · LocalLLaMA (pendiente de acceso)', 'https://www.reddit.com/r/LocalLLaMA/.rss?limit=20', 'china', 5),
]


def apply(data):
    sections = section_list(data, 'sections')
    existing = {s['key']: s for s in sections}
    additions = [
        {'key': 'ai', 'label': 'IA y modelos', 'icon': 'chip', 'tab': True},
        {'key': 'china', 'label': 'Modelos chinos', 'icon': 'flask', 'tab': False},
        {'key': 'harness', 'label': 'Agentes y harness', 'icon': 'chip', 'tab': False},
    ]
    for section in additions:
        if section['key'] not in existing:
            sections.append(section)
    for section in sections:
        if section['key'] == 'providers':
            section['label'] = 'Mercado Libre'
        elif section['key'] == 'tech':
            section['tab'] = False
    order = ['ai', 'china', 'harness', 'providers', 'news', 'world', 'tech']
    sections.sort(key=lambda s: order.index(s['key']) if s['key'] in order else len(order))
    feeds = section_list(data, 'feeds')
    ids = {f['id'] for f in feeds}
    urls = {f['url'] for f in feeds}
    added = 0
    for fid, name, url, category, cap in FEEDS:
        if fid in ids or url in urls:
            continue
        feeds.append({'id': fid, 'name': name, 'url': url, 'category': category,
                      'daily_cap': cap, 'enabled': fid != 'reddit-localllama'})
        added += 1
    print(f'Seguimientos añadidos: {added}. Total de fuentes: {len(feeds)}.')


if __name__ == '__main__':
    edit_config(apply)
    interests = SUPPORT / 'config' / 'interests.md'
    block = ('\n\n## Seguimientos solicitados\n'
             '- IA: nuevos modelos, capacidades, benchmarks y aplicaciones prácticas.\n'
             '- Modelos chinos: DeepSeek, Qwen, Kimi/Moonshot, GLM/Zhipu y MiniMax.\n'
             '- Agentes y harness: novedades y funciones de Codex, Claude Code, OpenCode y Hermes Agent.\n'
             '- Mercado Libre y Mercado Pago: tecnología, productos, negocio, resultados y novedades relevantes.\n'
             '- Combinar anuncios oficiales, portales especializados y perspectivas de Medium, Reddit y X.\n')
    text = interests.read_text() if interests.exists() else '## Want\n'
    if '## Seguimientos solicitados' not in text:
        write_profile('interests.md', text.rstrip() + block)
    print('Reddit queda pausado: el RSS devolvió HTTP 404. X usa publicaciones públicas indexadas.')
