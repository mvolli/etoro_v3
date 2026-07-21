"""QMD-Anbindung — semantischer Recall+Reindex der Trading-Insights.

feat/qmd-memory (2026-07-22): Der Bot schrieb seine Erkenntnisse zwar nach
docs/llm_insights.md (von QMD indexiert), las sie aber NIE zurueck — jeder
Review-Lauf begann ohne semantischen Zugriff auf die eigene Lern-Historie.
Dieses Modul gibt den LLM-Advisorn Lesezugriff (qmd_recall) und macht frisch
geschriebene Insights sofort durchsuchbar (qmd_reindex). Alles fail-open:
fehlt das qmd-Binary oder schlaegt der Call fehl, laeuft der Review normal
weiter (nur ohne QMD-Kontext).
"""
from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_QMD_BINARY = "/home/mvolli/.local/bin/qmd"
DEFAULT_COLLECTION = "hermes-workspace"


def qmd_recall(
    query: str,
    *,
    limit: int = 5,
    binary: str = DEFAULT_QMD_BINARY,
    collection: str = DEFAULT_COLLECTION,
    min_score: float = 0.03,
    timeout: float = 12.0,
) -> list[dict]:
    """Semantische Suche in der QMD-Wissensbasis. Gibt [] bei jedem Problem.

    Rueckgabe: Liste von {score, file, title, snippet} ueber min_score.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        proc = subprocess.run(
            [binary, "search", q, "--collection", collection,
             "--limit", str(limit), "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        rows = json.loads(proc.stdout)
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                score = float(r.get("score", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score < min_score:
                continue
            out.append({
                "score": round(score, 3),
                "file": str(r.get("file", ""))[:120],
                "title": str(r.get("title", ""))[:120],
                "snippet": str(r.get("snippet", "")).replace("\n", " ")[:400],
            })
        return out
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("qmd_recall failed: %s", exc)
        return []


def qmd_recall_block(queries, *, max_items: int = 6, **kwargs) -> str:
    """qmd_recall ueber EINE oder MEHRERE Queries -> fertiger Prompt-Block.

    QMD nutzt AND-Semantik: lange Queries (>4 Woerter) treffen oft nichts.
    Daher mehrere KURZE fokussierte Queries, Ergebnisse nach file dedupliziert
    und nach Score sortiert. Leerer String wenn nichts gefunden.
    """
    if isinstance(queries, str):
        queries = [queries]
    by_file: dict[str, dict] = {}
    for q in queries:
        for h in qmd_recall(q, **kwargs):
            key = h["file"] or h["title"]
            if key not in by_file or h["score"] > by_file[key]["score"]:
                by_file[key] = h
    if not by_file:
        return ""
    hits = sorted(by_file.values(), key=lambda h: -h["score"])[:max_items]
    lines = ["## Erinnerte Insights (QMD-Recall aus der eigenen Wissensbasis)"]
    for h in hits:
        src = (h["title"] or h["file"]).split("/")[-1]
        lines.append(f"- [{src}] {h['snippet']}")
    return "\n".join(lines)


def qmd_reindex(
    *,
    binary: str = DEFAULT_QMD_BINARY,
    collection: str = DEFAULT_COLLECTION,
    timeout: float = 30.0,
) -> bool:
    """Frisch geschriebene Insights sofort durchsuchbar machen (qmd update).

    Sonst waeren neue docs/llm_insights.md-Eintraege erst nach dem naechtlichen
    02:30-Refresh auffindbar. Fail-open.
    """
    try:
        proc = subprocess.run(
            [binary, "update", "--collection", collection],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("qmd_reindex failed: %s", exc)
        return False
