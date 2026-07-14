"""llm_client.py — gemeinsamer llama-server-JSON-Client fuer alle LLM-Advisors.

Buendelt das Muster aus llm_review_worker/position_review_worker:
  - POST an den lokalen llama-server (Qwen3, /no_think, JSON-Mode)
  - content/reasoning_content-Fallback + JSON-Extraktion
  - Fast-Retry (fix/llm-fast-retry): nur SCHNELL-Fails (<10s, z.B. connection
    refused bei Modell-Reload) werden bis 2x mit 15s/30s Pause wiederholt.
    Ein voller Timeout wird NICHT wiederholt (120s-no_agent-Cron-Budget).

Alle Aufrufer sind fail-open: None-Rueckgabe darf NIE einen Worker crashen.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"


def _call_once(prompt: str, system: str, max_tokens: int, temperature: float,
               timeout_s: float, label: str) -> dict | None:
    payload = json.dumps({
        "model": "local",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    try:
        req = urllib.request.Request(
            LLM_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
            msg = data["choices"][0]["message"]
            content = msg.get("content", "") or msg.get("reasoning_content", "") or ""
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
            logger.warning("[%s] Kein JSON in LLM-Antwort (len=%d)", label, len(content))
    except urllib.error.URLError as e:
        logger.warning("[%s] LLM nicht erreichbar: %s", label, e)
    except json.JSONDecodeError as e:
        logger.warning("[%s] JSON-Parse-Fehler: %s", label, e)
    except Exception as e:
        logger.warning("[%s] LLM-Fehler: %s", label, e)
    return None


def call_llm_json(prompt: str,
                  system: str = "Du bist ein JSON-API fuer Trading-Analyse. "
                                "Antworte AUSSCHLIESSLICH mit validem JSON.",
                  max_tokens: int = 1024,
                  temperature: float = 0.1,
                  timeout_s: float = 60.0,
                  label: str = "llm") -> dict | None:
    """JSON-Call mit Fast-Retry. None bei endgueltigem Fehlschlag (fail-open)."""
    for attempt in range(3):
        t0 = time.monotonic()
        result = _call_once(prompt, system, max_tokens, temperature, timeout_s, label)
        if result is not None:
            return result
        elapsed = time.monotonic() - t0
        if elapsed > 10.0 or attempt >= 2:
            return None
        wait = (15, 30)[min(attempt, 1)]
        logger.info("[%s] LLM-Schnell-Fail nach %.1fs — Retry in %ds", label, elapsed, wait)
        time.sleep(wait)
    return None
