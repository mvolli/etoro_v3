#!/bin/bash
# etoro_kill_switch_watchdog.sh — Kill Switch + Worker-Heartbeat Watchdog (V3)
#
# Cron: */5 * * * *  →  no-agent, discord:1513971015108263957
# Gibt NUR dann Output zurück, wenn etwas nicht stimmt (Kill Switch aktiv
# oder Worker-Heartbeat stale). Leerer Output = kein Discord-Post.
#
# fix/kill-switch-path-unification: Source of Truth ist data/kill_switch.flag
# (bot.core.kill_switch) — NICHT /tmp/etoro_kill_switch. /tmp wird von WSL
# beim Reboot geleert.
#
# fix/monitor-self-heartbeat: Dieser Watchdog ist die EXTERNE Instanz des
# Dead-Man's-Switch. Der monitor_worker prueft die anderen Worker, aber wer
# prueft den Monitor? → Dieses Script, alle 5 Minuten, unabhaengig von den
# Workern. Bekannte Grenze: stirbt der Hermes-Cron-Daemon selbst, laeuft
# auch dieses Script nicht mehr (Single Point of Failure: Hermes-Cron).

KILL_SWITCH_FILE="/home/mvolli/.hermes/workspace/etoro_v3/data/kill_switch.flag"
LEGACY_FILE="/tmp/etoro_kill_switch"
DB="/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db"
STALE_ALERT_MARKER="/home/mvolli/.hermes/workspace/etoro_v3/data/.watchdog_stale_last_alert"
STALE_ALERT_COOLDOWN_S=1800   # max. 1 Stale-Alert pro 30 Min (kein 5-min-Spam)

# ── 1. Kill-Switch-Check ──────────────────────────────────────────────────────
# Migration: ein versehentlich am Legacy-Pfad angelegter Kill-Switch wird
# an den echten Pfad übernommen (Operator-Intention respektieren).
if [ -f "$LEGACY_FILE" ] && [ ! -f "$KILL_SWITCH_FILE" ]; then
    cp "$LEGACY_FILE" "$KILL_SWITCH_FILE" 2>/dev/null
    echo "⚠️ Kill Switch am Legacy-Pfad ${LEGACY_FILE} gefunden — nach ${KILL_SWITCH_FILE} migriert."
fi

if [ -f "$KILL_SWITCH_FILE" ]; then
    # Flag kann JSON ({reason, scope, tripped_at}) oder Legacy-Plaintext sein.
    # Scope (ein Token) zuerst, Rest der Zeile = Reason (darf Leerzeichen enthalten).
    read -r SCOPE REASON <<EOF2
$(python3 - "$KILL_SWITCH_FILE" 2>/dev/null <<'PYEOF'
import json, sys
raw = open(sys.argv[1]).read().strip()
try:
    d = json.loads(raw)
    print(f"{d.get('scope','manual')} {d.get('reason','?')}")
except Exception:
    print(f"manual {raw or 'Unbekannt'}")
PYEOF
)
EOF2
    [ -z "$REASON" ] && REASON=$(cat "$KILL_SWITCH_FILE" 2>/dev/null || echo "Unbekannt")
    [ -z "$SCOPE" ] && SCOPE="manual"
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "🔴 KILL SWITCH AKTIV — eToro Trading Bot V3"
    echo "Zeitpunkt: ${TIMESTAMP}"
    echo "Grund: ${REASON}"
    echo "Scope: ${SCOPE}"
    echo "Aktion: CRITICAL-Regime erzwungen | Alle neuen BUYs blockiert"
    if [ "$SCOPE" = "daily" ]; then
        echo "Auto-Reset: am nächsten UTC-Handelstag (risk_worker)"
    else
        echo "Deaktivieren: rm ${KILL_SWITCH_FILE}"
    fi
fi

# ── 2. Worker-Heartbeat-Staleness (extern, unabhängig vom monitor_worker) ────
# Schwelle = 3× Cron-Intervall (synchron zu bot/core/heartbeat.py STALE_FACTOR).
if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    STALE=$(sqlite3 -readonly "$DB" "
        SELECT key || ': letzter Lauf ' || value || ' UTC ('
               || CAST(ROUND((julianday('now') - julianday(value)) * 1440) AS INTEGER)
               || ' min alt, Limit ' || lim || ' min)'
        FROM (
            SELECT key, value,
                   CASE key
                       WHEN 'LAST_RUN_DATA_WORKER'      THEN 15
                       WHEN 'LAST_RUN_RISK_WORKER'      THEN 15
                       WHEN 'LAST_RUN_RECONCILER'       THEN 15
                       WHEN 'LAST_RUN_SIGNAL_WORKER'    THEN 45
                       WHEN 'LAST_RUN_EXECUTION_WORKER' THEN 45
                       WHEN 'LAST_RUN_MONITOR_WORKER'   THEN 90
                       WHEN 'LAST_RUN_DISCOVERY_WORKER' THEN 360
                   END AS lim
            FROM system_state
            WHERE key LIKE 'LAST_RUN_%'
        )
        WHERE lim IS NOT NULL
          AND (julianday('now') - julianday(value)) * 1440 > lim
    " 2>/dev/null)

    if [ -n "$STALE" ]; then
        NOW_EPOCH=$(date +%s)
        LAST_ALERT=0
        [ -f "$STALE_ALERT_MARKER" ] && LAST_ALERT=$(cat "$STALE_ALERT_MARKER" 2>/dev/null || echo 0)
        if [ $((NOW_EPOCH - LAST_ALERT)) -ge $STALE_ALERT_COOLDOWN_S ]; then
            echo "$NOW_EPOCH" > "$STALE_ALERT_MARKER"
            echo "💀 WORKER-HEARTBEAT STALE — eToro Trading Bot V3"
            echo "$STALE"
            echo "Aktion: Hermes-Cron prüfen (~/.hermes/cron/jobs.json) | Worker-Logs: ~/.hermes/cron/output/"
        fi
    else
        # Alles gesund → Cooldown-Marker zurücksetzen, damit ein NEUER
        # Ausfall sofort (nicht erst nach Rest-Cooldown) alarmiert.
        rm -f "$STALE_ALERT_MARKER" 2>/dev/null
    fi
fi
# Kein Output → kein Discord-Post (Bot läuft normal)
