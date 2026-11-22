"""Final-review regression probes (synthetic only; no model or network calls).

The original defects are reproduced here and now assert the fixed behaviour, including the
capitalized-opener / publisher-suffix context matches fixed in ``newsnest/podcast_context.py``.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newsnest.podcast import Story
from newsnest.podcast_context import related_context
from tests.conftest import ROOT
from tests.test_podcast import add_story

SCRIPT = ROOT / "scripts" / "podcast-editor.py"
DAY = date(2026, 9, 15)
TZ = ZoneInfo("America/Santiago")


@pytest.fixture
def editor_module():
    spec = importlib.util.spec_from_file_location("podcast_editor_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _story(title: str) -> Story:
    return Story(title=title, snippet="", category="tech",
                 published=datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc),
                 score=0.0, publisher="Medios", sources=[])


def _published(days_before: int) -> datetime:
    return (datetime.combine(DAY - timedelta(days=days_before), datetime.min.time(), TZ)
            .replace(hour=12).astimezone(timezone.utc))


def _segment(sid, text):
    return {"id": sid, "text": text}


def _narration(word):
    return " ".join([word] * 29) + " nota."


def _run_with_last_message(editor_module, monkeypatch, payload: str):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        last = Path(command[command.index("--output-last-message") + 1])
        last.write_text(payload, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(editor_module.subprocess, "run", fake_run)
    return captured


def _codex_edit(editor_module):
    return editor_module.edit(
        [{"id": 1, "title": "Titulo", "snippet": "Texto", "publisher": "Medio"}],
        "", provider="codex", api_model="gpt-6-luna", max_tokens=1400, temperature=0.2)


# --- P1 (fixed): capitalized sentence opener and publisher suffix are not context ---------------

def test_generic_capitalized_opener_creates_false_historical_context(env, cfg, conn):
    """'Anunció' is a capitalised verb, not an entity; it must not link unrelated stories.

    newsnest/podcast_context.py:_entities() only filters _GENERIC_ENTITIES/_STOPWORDS. Spanish
    headlines start with a capitalised verb ('Anunció', 'Reportó', 'Informó', ...), so two
    unrelated stories share a spurious 'entity' and the past one is injected as context.
    """
    add_story(conn, cfg, title="Anunció recortes en regiones", published=_published(1))
    add_story(conn, cfg, title="Anunció nueva medida tributaria", published=_published(2))
    context = related_context(conn, _story("Anunció nueva medida tributaria"), DAY, tz=TZ)
    titles = [entry["title"] for entry in context]
    # The shared capitalised verb is noise; the identical old headline is valid context via its
    # two meaningful keywords ("medida", "tributaria"), so it must still be returned.
    assert "Anunció recortes en regiones" not in titles, f"unrelated story leaked: {context}"
    assert "Anunció nueva medida tributaria" in titles, f"valid 2-keyword context lost: {context}"


# --- P2 (fixed): numeral grounding compares exact values, not substrings ------------------------

def test_invented_number_hidden_inside_a_real_number_is_rejected(editor_module):
    story = {"id": 1, "title": "Presupuesto",
             "snippet": "Anunció 120 millones de inversión.", "publisher": "Medio"}
    invented = ("El ministerio anunció una inversión de 20 millones de pesos para las regiones "
                "y las comunas del país durante este año.")
    assert editor_module._numeric_values(invented) == {20}
    assert editor_module._numeric_values(editor_module._story_source(story)) == {120}
    with pytest.raises(ValueError, match="invents numbers"):
        editor_module.validate_batch(json.dumps([_segment(1, invented)]), [story])


def test_numeral_formatting_is_canonicalized_and_grounded(editor_module):
    """'1,000' -> '1.000', a '2026-2030' range grounds '2030', trailing dots ignored."""
    batch = [
        {"id": 1, "title": "Producto A", "snippet": "Producto A $20 y 1,000 credits.",
         "publisher": "Medio"},
        {"id": 2, "title": "Pronóstico", "snippet": "El periodo 2026-2030 trae cambios.",
         "publisher": "Medio"},
    ]
    first = ("El producto A cuesta 20 dólares y ofrece 1.000 créditos a los usuarios "
             "de la plataforma durante el próximo mes en la región.")
    second = ("El pronóstico del periodo 2030 muestra cambios claros para todas las regiones "
              "del país durante los próximos años y también para los sectores productivos.")
    assert editor_module._numeric_values("1,000 credits / 1.000 créditos / 2030.") == {1000, 2030}
    result = editor_module.validate_batch(
        json.dumps([_segment(1, first), _segment(2, second)]), batch)
    assert [segment["id"] for segment in result] == [1, 2]

    novel = json.dumps([_segment(1, first.replace("1.000 créditos", "2.000 créditos")),
                        _segment(2, second.replace("2030", "2031"))])
    with pytest.raises(ValueError):
        editor_module.validate_batch(novel, batch)


# --- P2 (fixed): codex duplicate keys rejected by the strict parser -----------------------------

def test_codex_output_duplicate_json_keys_are_rejected(editor_module, monkeypatch):
    good = _narration("noticia")
    raw = ('{"segments":[{"id":1,"text":"<think>ignora instrucciones</think>",'
           '"text":"' + good + '"}]}')
    _run_with_last_message(editor_module, monkeypatch, raw)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        _codex_edit(editor_module)


# --- Subscription-only auth: force the ChatGPT login method in argv -----------------------------

def test_codex_forces_chatgpt_login_method(editor_module, monkeypatch):
    """Official config reference: forced_login_method = chatgpt | api (restrict auth method)."""
    captured = _run_with_last_message(
        editor_module, monkeypatch,
        json.dumps({"segments": [_segment(1, _narration("noticia"))]}))
    assert _codex_edit(editor_module)[0]["id"] == 1
    command = captured["command"]
    assert "forced_login_method=chatgpt" in command
    assert command[command.index("forced_login_method=chatgpt") - 1] == "-c"


# --- Prompt: no filler about input metadata, no word-floor padding ------------------------------

def test_system_prompt_targets_natural_sentences_and_forbids_metadata_filler(editor_module):
    system = editor_module.SYSTEM_PROMPT.lower()
    assert "1 a 3 oraciones" in system
    assert "no hay cuota de palabras" in system
    # One clear prohibition keeps the model from narrating the input fields or missing details.
    assert "nunca menciones el titular" in system
    for phrase in ("fragmento", "texto de entrada", "falta de detalles", "contexto útil",
                   "atribuye las acusaciones a su fuente"):
        assert phrase in system, phrase
    assert "25 palabras" not in system and "25 y 40" not in system


# --- P1 fix guardrails: reporting verbs excluded, real brands still related --------------------

def test_reporting_verb_does_not_create_false_context(env, cfg, conn):
    add_story(conn, cfg, title="Reportó ganancias la empresa", published=_published(1))
    context = related_context(conn, _story("Reportó pérdidas el banco"), DAY, tz=TZ)
    assert context == [], f"reporting verb leaked as entity: {context}"


def test_brands_still_relate_as_context(env, cfg, conn):
    add_story(conn, cfg, title="DeepSeek y Qwen renuevan modelos", published=_published(2))
    context = related_context(conn, _story("Qwen y DeepSeek mejoran razonamiento"), DAY, tz=TZ)
    assert len(context) == 1 and "DeepSeek" in context[0]["title"]
