"""Plain-language explanation via Gemini (free tier).

The model receives ONLY numbers already computed by the engine and must not invent
levels. Its output is treated as untrusted: strict schema, type checks, length limits,
control characters stripped. The site renders it as plain text (never HTML).
If Gemini is unavailable, the analysis is still published without the text.
"""
from __future__ import annotations

import json
import os
import re

from .http import FetchError, request_json
from .sources import SourceStatus, now_iso

_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,60}$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
MAX_LEN = 900
DEFAULT_MODELS = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite"]

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "W": {"type": "STRING"},
        "D": {"type": "STRING"},
        "H4": {"type": "STRING"},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "next_week": {"type": "STRING"},
    },
    "required": ["summary", "W", "D", "H4", "risks", "next_week"],
}

PROMPT = """Sei l'analista macro di "Overmind", uno strumento personale di analisi dell'oro (XAUUSD).
Ricevi in JSON un'analisi GIÀ CALCOLATA dal software (bias, confidenza %, voti dei fattori, zone).
Compito: spiegare in italiano semplice PERCHÉ il quadro è questo, per ogni timeframe.

Regole tassative:
- Usa SOLO numeri e livelli presenti nel JSON. Non inventare prezzi, date, notizie o eventi.
- Non cambiare bias o confidenza: spiegali. Se la confidenza è bassa, dì chiaramente che i segnali sono in conflitto e quali.
- Collega i fattori (es. rendimenti reali in salita = costo opportunità dell'oro più alto; dollaro forte = oro più caro per chi compra in altre valute; fondi molto long = rischio prese di profitto).
- Non dare ordini operativi (niente "compra"/"vendi"): descrivi scenari e livelli di invalidazione.
- "next_week": 2-4 frasi sulla previsione della settimana successiva (direzione, probabilità, range, scenario
  alternativo ed eventi chiave), usando solo i dati di "previsione_settimana_successiva".
- Max 3 frasi per timeframe, max 3 frasi per il riassunto, 2-4 rischi brevi.

Dati:
"""


def _clean(s, limit=MAX_LEN) -> str:
    if not isinstance(s, str):
        raise ValueError("campo non testuale")
    s = _CTRL.sub("", s).strip()
    return s[:limit]


def validate(obj) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("output non oggetto")
    out = {k: _clean(obj.get(k)) for k in ("summary", "W", "D", "H4")}
    risks = obj.get("risks")
    if not isinstance(risks, list):
        raise ValueError("risks non lista")
    out["risks"] = [_clean(r, 300) for r in risks[:5] if isinstance(r, str) and r.strip()]
    nw = obj.get("next_week", "")
    out["next_week"] = _clean(nw) if isinstance(nw, str) else ""
    if not out["summary"]:
        raise ValueError("riassunto vuoto")
    return out


def explain(payload: dict) -> tuple[dict | None, SourceStatus]:
    st = SourceStatus("gemini", "Spiegazione IA (Gemini)")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    custom = os.environ.get("GEMINI_MODEL", "").strip()
    if not key:
        st.status, st.message = "disabled", "Chiave GEMINI_API_KEY non configurata: nessuna spiegazione testuale"
        return None, st
    if custom and not _MODEL_RE.match(custom):
        st.status, st.message = "down", "Nome modello GEMINI_MODEL non valido"
        return None, st
    # "-latest" aliases follow Google's model retirements automatically; try in order.
    models = list(dict.fromkeys(([custom] if custom else []) + DEFAULT_MODELS))
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                             "temperature": 0.3, "maxOutputTokens": 4096},
    }
    errors = []
    for model in models:
        try:
            js = request_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key}, body=body, timeout=90)
            text = js["candidates"][0]["content"]["parts"][0]["text"]
            result = validate(json.loads(text))
        except FetchError as e:
            errors.append(f"{model}: {e}")
            continue
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            errors.append(f"{model}: risposta non valida")
            continue
        st.last_success = now_iso()
        st.detail = {"model": model}
        if errors:
            st.status = "degraded"
            st.message = f"Usato il modello di riserva {model} ({'; '.join(errors)})"
        return result, st
    st.status = "down"
    st.message = f"Gemini non disponibile ({'; '.join(errors)}): mostro solo l'analisi calcolata"
    return None, st
