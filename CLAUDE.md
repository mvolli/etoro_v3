# CLAUDE.md — etoro_v3

Lies und befolge `AGENTS.md` in diesem Verzeichnis (DOX-Child-Vertrag) sowie
das Root-Rail `~/.hermes/workspace/AGENTS.md` (inkl. NIEMALS-Liste).

Kurzfassung der harten Regeln:
- LIVE-System mit echtem Geld — läuft während jeder Änderung weiter.
- Einzige DB: `data/trading.db`. Tests: `PYTHONPATH=src python3 -m pytest` (alle grün).
- Geldwirksame Schwellen nur nach expliziter User-Entscheidung ändern.
- Nach jeder bedeutsamen Änderung: DOX-Pass (AGENTS.md aktuell? Veraltetes löschen)
  und Live-Verifikation (Heartbeats/Watchdog), nicht nur Tests.
