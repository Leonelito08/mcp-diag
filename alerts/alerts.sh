#!/usr/bin/env bash
# ============================================================================
# alerts.sh — Capa de alertas EVA/TDH (Telegram)
#
# Uso:  bash alerts.sh test      # manda mensaje de prueba al Telegram
#       bash alerts.sh check     # corre todos los chequeos, alerta si hay problemas
#       bash alerts.sh install   # instala el cron (cada 10 min) de forma idempotente
#
# Chequeos:
#   1. Disco raíz >= DISK_THRESHOLD %
#   2. Servicios swarm con réplicas caídas (ej. 0/1)
#   3. Chatwoot no responde vía Traefik local (no 2xx/3xx)
#   4. Heartbeat de negocio (solo 09-23 hs): conversaciones cuyo último mensaje
#      es del cliente hace > HEARTBEAT_MINUTES, en estado pending sin asignar
#      — o sea, Manu debería haber respondido y no lo hizo.
#
# Config: /opt/mcp-diag/alerts/alerts.env (copiar de alerts.env.example)
# ============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ENVF="$DIR/alerts.env"
if [ ! -f "$ENVF" ]; then echo "ERROR: falta $ENVF (copiá alerts.env.example)"; exit 1; fi
set -a; . "$ENVF"; set +a

: "${TELEGRAM_TOKEN:?Falta TELEGRAM_TOKEN en alerts.env}"
: "${TELEGRAM_CHAT_ID:?Falta TELEGRAM_CHAT_ID en alerts.env}"
DISK_THRESHOLD="${DISK_THRESHOLD:-90}"
HEARTBEAT_MINUTES="${HEARTBEAT_MINUTES:-12}"
CHATWOOT_LOCAL_URL="${CHATWOOT_LOCAL_URL:-http://127.0.0.1:80}"
CHATWOOT_HOST_HEADER="${CHATWOOT_HOST_HEADER:-}"
NEON_URL="${NEON_URL:-}"
PG_FILTER="${PG_FILTER:-pgvector_pgvector}"

send() {
  curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode text="$1" >/dev/null || true
}

case "${1:-}" in
# ----------------------------------------------------------------------------
test)
  send "✅ Canal de alertas EVA operativo — $(hostname), $(date '+%Y-%m-%d %H:%M')"
  echo "Mensaje de prueba enviado. Fijate en Telegram."
  ;;
# ----------------------------------------------------------------------------
install)
  ( crontab -l 2>/dev/null | grep -v 'alerts.sh check' ; \
    echo "*/10 * * * * bash $DIR/alerts.sh check >> /var/log/eva-alertas.log 2>&1" ) | crontab -
  echo "Cron instalado: chequeo cada 10 minutos. Log: /var/log/eva-alertas.log"
  crontab -l | grep alerts.sh
  ;;
# ----------------------------------------------------------------------------
check)
  PROBLEMS=()

  # 1) Disco
  USO=$(df --output=pcent / | tail -1 | tr -dc '0-9')
  if [ -n "$USO" ] && [ "$USO" -ge "$DISK_THRESHOLD" ]; then
    PROBLEMS+=("💾 Disco al ${USO}% (umbral ${DISK_THRESHOLD}%)")
  fi

  # 2) Servicios swarm
  BAD=$(docker service ls --format '{{.Name}} {{.Replicas}}' 2>/dev/null | \
        awk '{n=split($2,a,"/"); if (n==2 && a[1]!=a[2]) print "• "$1" ("$2")"}')
  if [ -n "$BAD" ]; then
    PROBLEMS+=("🔴 Servicios caídos:\n$BAD")
  fi

  # 3) Chatwoot vía Traefik local
  args=(-s -o /dev/null -m 10 -w '%{http_code}')
  [ -n "$CHATWOOT_HOST_HEADER" ] && args+=(-H "Host: $CHATWOOT_HOST_HEADER")
  CODE=$(curl "${args[@]}" "$CHATWOOT_LOCAL_URL" 2>/dev/null || echo 000)
  case "$CODE" in
    2*|3*) : ;;
    *) PROBLEMS+=("🟠 Chatwoot no responde local (HTTP $CODE)") ;;
  esac

  # 4) Heartbeat de negocio (solo 09-23 hs, hora local del server)
  H=$((10#$(date +%H)))
  if [ -n "$NEON_URL" ] && [ "$H" -ge 9 ] && [ "$H" -le 23 ]; then
    CID=$(docker ps -q -f "name=${PG_FILTER}" | head -1)
    if [ -n "$CID" ]; then
      SQL=$(cat <<'EOSQL'
WITH last_msgs AS (
  SELECT DISTINCT ON ("conversationId") "conversationId", role::text AS role, "createdAt"
  FROM "AgentMessage" WHERE "obsoletedAt" IS NULL
  ORDER BY "conversationId", "createdAt" DESC
)
SELECT count(*) FROM last_msgs lm
JOIN "AgentConversation" c ON c.id = lm."conversationId"
WHERE lm.role = 'USER'
  AND lm."createdAt" < now() - (:'mins')::interval
  AND lm."createdAt" > now() - interval '24 hours'
  AND c."lastKnownChatwootStatus" = 'pending'
  AND c."lastKnownAssigneeId" IS NULL;
EOSQL
)
      MUDAS=$(docker exec "$CID" psql "$NEON_URL" -v mins="${HEARTBEAT_MINUTES} minutes" -tAc "$SQL" 2>/dev/null | tr -dc '0-9')
      if [ -z "$MUDAS" ]; then
        PROBLEMS+=("⚠️ Heartbeat: no pude consultar Neon (¿conexión/URL?)")
      elif [ "$MUDAS" -gt 0 ]; then
        PROBLEMS+=("🔇 MANU MUDO: $MUDAS conversación(es) de cliente sin respuesta hace >${HEARTBEAT_MINUTES} min (pending, sin asignar)")
      fi
    fi
  fi

  # Resultado
  if [ "${#PROBLEMS[@]}" -gt 0 ]; then
    MSG="🚨 EVA Alertas — $(hostname) $(date '+%H:%M')"
    for p in "${PROBLEMS[@]}"; do MSG="$MSG\n\n$p"; done
    send "$(printf '%b' "$MSG")"
    printf '%b\n' "$MSG"
    exit 1
  else
    echo "$(date '+%F %H:%M') OK — disco ${USO}%, servicios completos, chatwoot HTTP ${CODE}"
  fi
  ;;
# ----------------------------------------------------------------------------
*)
  echo "Uso: bash alerts.sh {test|check|install}"
  exit 1
  ;;
esac
