#!/bin/bash
# etoro_kill_switch_watchdog.sh — Kill Switch Watchdog für eToro Trading Bot V3
#
# Cron: */5 * * * *  →  no-agent, discord:1513971015108263957
# Gibt NUR dann Output zurück, wenn der Kill Switch aktiv ist.
# Leerer Output = kein Discord-Post (normaler Betrieb).
#
# fix/kill-switch-path-unification: Source of Truth ist data/kill_switch.flag
# (bot.core.kill_switch) — NICHT /tmp/etoro_kill_switch. Der alte /tmp-Pfad
# wurde von keinem Worker gelesen: der Watchdog alarmierte nie beim echten
# Kill-Switch, und ein per Discord-Anleitung angelegtes /tmp-File stoppte
# den Bot nicht. /tmp wird von WSL beim Reboot geleert.

KILL_SWITCH_FILE="/home/mvolli/.hermes/workspace/etoro_v3/data/kill_switch.flag"
LEGACY_FILE="/tmp/etoro_kill_switch"

# Migration: ein versehentlich am Legacy-Pfad angelegter Kill-Switch wird
# an den echten Pfad übernommen (Operator-Intention respektieren).
if [ -f "$LEGACY_FILE" ] && [ ! -f "$KILL_SWITCH_FILE" ]; then
    cp "$LEGACY_FILE" "$KILL_SWITCH_FILE" 2>/dev/null
    echo "⚠️ Kill Switch am Legacy-Pfad ${LEGACY_FILE} gefunden — nach ${KILL_SWITCH_FILE} migriert."
fi

if [ -f "$KILL_SWITCH_FILE" ]; then
    REASON=$(cat "$KILL_SWITCH_FILE" 2>/dev/null || echo "Unbekannt")
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "🔴 KILL SWITCH AKTIV — eToro Trading Bot V3"
    echo "Zeitpunkt: ${TIMESTAMP}"
    echo "Grund: ${REASON}"
    echo "Aktion: CRITICAL-Regime erzwungen | Alle neuen BUYs blockiert"
    echo "Deaktivieren: rm ${KILL_SWITCH_FILE}"
fi
# Kein Output → kein Discord-Post (Bot läuft normal)
