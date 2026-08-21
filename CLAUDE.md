# CLAUDE.md — etoro_v3

Lies und befolge `AGENTS.md` in diesem Verzeichnis (DOX-Child-Vertrag) sowie
das Root-Rail `~/.hermes/workspace/AGENTS.md`. Die NIEMALS-Liste steht seit
2026-08-21 im Original in `AGENTS.md` — das Root-Rail liegt ausserhalb des
Git-Roots und erreicht den Agent-Prompt nicht.

Kurzfassung der harten Regeln:
- LIVE-System mit echtem Geld — läuft während jeder Änderung weiter.
- Einzige DB: `data/trading.db`. Tests: `PYTHONPATH=src /usr/bin/python3 -m pytest`
  (alle grün). Der Interpreter zählt — bares `python3` ist das hermes-agent-venv
  ohne matplotlib, alle Chart-Tests scheitern dort leise. Details in AGENTS.md.
- Geldwirksame Schwellen nur nach expliziter User-Entscheidung ändern.
- Nach jeder bedeutsamen Änderung: DOX-Pass (AGENTS.md aktuell? Veraltetes löschen)
  und Live-Verifikation (Heartbeats/Watchdog), nicht nur Tests.
