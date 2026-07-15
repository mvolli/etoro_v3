#!/bin/bash
# etoro_kill_switch_watchdog.sh — Kill Switch + Worker-Heartbeat Watchdog (V3)
#
# Cron: */5 * * * *  →  no-agent, discord:1513971015108263957
# Gibt NUR dann Output zurück, wenn etwas nicht stimmt (Kill Switch aktiv,
# Self-Healing ausgeführt oder Eskalation). Leerer Output = kein Discord-Post.
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
#
# fix/watchdog-self-healing (2026-07-14): stale Worker werden zuerst EINMAL
# selbst neu gestartet (worker_lock verhindert Doppellaeufe; nohup entkoppelt
# vom 120s-Cron-Budget). Eskalations-Alert (💀) nur, wenn der Worker
# HEAL_VERIFY_GRACE_S nach dem Heilversuch immer noch stale ist.
# Zusaetzlich: LLM-Lernschleife-Freshness — ist llm_position_recommendations
# aelter als 26h und llama-server erreichbar, wird der Position-Review-Worker
# neu gestartet; aelter als 48h → Alert (Stufe 2 zu fix/llm-fast-retry).

KILL_SWITCH_FILE="/home/mvolli/.hermes/workspace/etoro_v3/data/kill_switch.flag"
LEGACY_FILE="/tmp/etoro_kill_switch"
DB="/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db"
SCRIPTS_DIR="/home/mvolli/.hermes/scripts"
HEAL_DIR="/home/mvolli/.hermes/workspace/etoro_v3/data/.watchdog_heal"
HEAL_LOG_DIR="/home/mvolli/.hermes/cron/output"
STALE_ALERT_MARKER="/home/mvolli/.hermes/workspace/etoro_v3/data/.watchdog_stale_last_alert"
STALE_ALERT_COOLDOWN_S=1800   # max. 1 Eskalations-Alert pro 30 Min (kein 5-min-Spam)
HEAL_RETRY_COOLDOWN_S=7200    # max. 1 Heilversuch pro Worker pro 2h
HEAL_VERIFY_GRACE_S=900       # 15 min Zeit fuer Heartbeat-Update nach Heilversuch

LLM_RECO_FILE="/home/mvolli/.hermes/workspace/etoro_v3/data/llm_position_recommendations.json"
LLM_URL_HEALTH="http://127.0.0.1:8080/health"
LLM_ALERT_MARKER="/home/mvolli/.hermes/workspace/etoro_v3/data/.watchdog_llm_last_alert"
LLM_ALERT_COOLDOWN_S=21600    # max. 1 LLM-Alert pro 6h
LLM_HEAL_COOLDOWN_S=43200     # max. 1 LLM-Review-Rerun pro 12h
LLM_STALE_HEAL_MIN=1560       # 26h → Selbstheilung (position_review laeuft 3x/Tag)
LLM_STALE_ALERT_MIN=2880      # 48h → Alert

mkdir -p "$HEAL_DIR" 2>/dev/null

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

# ── 2. Worker-Heartbeat-Staleness + Self-Healing ─────────────────────────────
# Schwelle = 3× Cron-Intervall (synchron zu bot/core/heartbeat.py STALE_FACTOR).
HEARTBEAT_SQL_BASE="
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
      AND (julianday('now') - julianday(value)) * 1440 > lim"

if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    STALE_KEYS=$(sqlite3 -readonly "$DB" "SELECT key ${HEARTBEAT_SQL_BASE}" 2>/dev/null)

    if [ -n "$STALE_KEYS" ]; then
        NOW_EPOCH=$(date +%s)
        HEALED=""
        ESCALATE=0
        for KEY in $STALE_KEYS; do
            WNAME=$(echo "$KEY" | sed 's/^LAST_RUN_//' | tr '[:upper:]' '[:lower:]')
            WSCRIPT="${SCRIPTS_DIR}/v3_${WNAME}.sh"
            MARKER="${HEAL_DIR}/${WNAME}"
            LAST_HEAL=0
            [ -f "$MARKER" ] && LAST_HEAL=$(cat "$MARKER" 2>/dev/null || echo 0)
            HEAL_AGE=$((NOW_EPOCH - LAST_HEAL))
            if [ "$HEAL_AGE" -lt "$HEAL_VERIFY_GRACE_S" ]; then
                :   # Heilversuch <15 min her — Heartbeat Zeit geben, still warten
            elif [ "$HEAL_AGE" -lt "$HEAL_RETRY_COOLDOWN_S" ]; then
                ESCALATE=1   # Heilversuch hat nicht geholfen → Alert
            elif [ -f "$WSCRIPT" ]; then
                echo "$NOW_EPOCH" > "$MARKER"
                nohup timeout 600 bash "$WSCRIPT" \
                    >> "${HEAL_LOG_DIR}/watchdog_heal_${WNAME}.log" 2>&1 &
                HEALED="${HEALED} ${WNAME}"
            else
                ESCALATE=1   # kein Worker-Script vorhanden — nicht heilbar
            fi
        done

        if [ -n "$HEALED" ]; then
            echo "🩹 SELF-HEALING — eToro Trading Bot V3"
            echo "Stale Worker neu gestartet:${HEALED}"
            echo "Log: ${HEAL_LOG_DIR}/watchdog_heal_<worker>.log | Eskalation, falls in 15 min weiter stale."
        fi

        if [ "$ESCALATE" -eq 1 ]; then
            LAST_ALERT=0
            [ -f "$STALE_ALERT_MARKER" ] && LAST_ALERT=$(cat "$STALE_ALERT_MARKER" 2>/dev/null || echo 0)
            if [ $((NOW_EPOCH - LAST_ALERT)) -ge $STALE_ALERT_COOLDOWN_S ]; then
                echo "$NOW_EPOCH" > "$STALE_ALERT_MARKER"
                STALE_DETAIL=$(sqlite3 -readonly "$DB" "
                    SELECT key || ': letzter Lauf ' || value || ' UTC ('
                           || CAST(ROUND((julianday('now') - julianday(value)) * 1440) AS INTEGER)
                           || ' min alt, Limit ' || lim || ' min)'
                    ${HEARTBEAT_SQL_BASE}" 2>/dev/null)
                echo "💀 WORKER-HEARTBEAT STALE (Self-Healing erfolglos) — eToro Trading Bot V3"
                echo "$STALE_DETAIL"
                echo "Aktion: Hermes-Cron prüfen (~/.hermes/cron/jobs.json) | Worker-Logs: ~/.hermes/cron/output/"
            fi
        fi
    else
        # Alles gesund → Marker zurücksetzen, damit ein NEUER Ausfall sofort
        # heilt/alarmiert (nicht erst nach Rest-Cooldown).
        rm -f "$STALE_ALERT_MARKER" 2>/dev/null
        rm -f "$HEAL_DIR"/* 2>/dev/null
    fi
fi

# ── 3. LLM-Lernschleife-Freshness (fix/llm-fast-retry, Stufe 2) ───────────────
# position_review_worker schreibt llm_position_recommendations.json 3x/Tag
# (08:30/14:30/19:30 CEST), llm_review_worker 1x/Tag. Aelter als 26h heisst:
# mindestens 3 Laeufe in Folge fehlgeschlagen (LLM down, Crash, Cron-Problem).
if [ -f "$LLM_RECO_FILE" ]; then
    LLM_AGE_MIN=$(( ( $(date +%s) - $(stat -c %Y "$LLM_RECO_FILE" 2>/dev/null || echo 0) ) / 60 ))
else
    LLM_AGE_MIN=99999
fi

if [ "$LLM_AGE_MIN" -gt "$LLM_STALE_HEAL_MIN" ]; then
    NOW_EPOCH=$(date +%s)
    LLM_SERVER_UP=0
    if command -v curl >/dev/null 2>&1 && curl -s -m 3 -o /dev/null "$LLM_URL_HEALTH" 2>/dev/null; then
        LLM_SERVER_UP=1
    fi

    if [ "$LLM_SERVER_UP" -eq 1 ]; then
        LLM_HEAL_MARKER="${HEAL_DIR}/llm_position_review"
        LAST_LLM_HEAL=0
        [ -f "$LLM_HEAL_MARKER" ] && LAST_LLM_HEAL=$(cat "$LLM_HEAL_MARKER" 2>/dev/null || echo 0)
        if [ $((NOW_EPOCH - LAST_LLM_HEAL)) -ge "$LLM_HEAL_COOLDOWN_S" ] \
           && [ -f "${SCRIPTS_DIR}/v3_position_review_worker.sh" ]; then
            echo "$NOW_EPOCH" > "$LLM_HEAL_MARKER"
            nohup timeout 600 bash "${SCRIPTS_DIR}/v3_position_review_worker.sh" \
                >> "${HEAL_LOG_DIR}/watchdog_heal_llm_position_review.log" 2>&1 &
            echo "🩹 SELF-HEALING — LLM-Review veraltet ($((LLM_AGE_MIN / 60))h), llama-server erreichbar"
            echo "position_review_worker neu gestartet. Log: ${HEAL_LOG_DIR}/watchdog_heal_llm_position_review.log"
        fi
    fi

    if [ "$LLM_AGE_MIN" -gt "$LLM_STALE_ALERT_MIN" ]; then
        LAST_LLM_ALERT=0
        [ -f "$LLM_ALERT_MARKER" ] && LAST_LLM_ALERT=$(cat "$LLM_ALERT_MARKER" 2>/dev/null || echo 0)
        if [ $((NOW_EPOCH - LAST_LLM_ALERT)) -ge "$LLM_ALERT_COOLDOWN_S" ]; then
            echo "$NOW_EPOCH" > "$LLM_ALERT_MARKER"
            echo "💀 LLM-LERNSCHLEIFE STALE — eToro Trading Bot V3"
            echo "llm_position_recommendations.json ist $((LLM_AGE_MIN / 60))h alt (Limit 48h)."
            if [ "$LLM_SERVER_UP" -eq 1 ]; then
                echo "llama-server: erreichbar — Worker-Problem? Logs: ${HEAL_LOG_DIR}/"
            else
                echo "llama-server: NICHT erreichbar (${LLM_URL_HEALTH}) — llama-server auf Windows-Seite starten (F:\\llama.cpp\\llama-server.exe)."
            fi
            echo "Hinweis: Bei 0 offenen Positionen kann die Datei legitim alt sein."
        fi
    fi
fi
# Kein Output → kein Discord-Post (Bot läuft normal)
