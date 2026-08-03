#!/usr/bin/env bash
# ============================================================================
# dedup.sh — Deduplicación de adjuntos de Chatwoot (Active Storage)
#
# Uso:  bash dedup.sh backup        # pg_dump de respaldo a /root (OBLIGATORIO primero)
#       bash dedup.sh test          # dedup de UN solo grupo chico (prueba)
#       bash dedup.sh files test    # borra archivos físicos del grupo de prueba
#       bash dedup.sh full          # dedup de TODOS los grupos duplicados
#       bash dedup.sh files full    # borra archivos físicos del dedup completo
#       bash dedup.sh status        # estado: grupos duplicados restantes + disco
#
# Qué hace test/full (en UNA transacción, aborta entera si algo falla):
#   1. Elige un blob canónico por grupo (checksum+byte_size), el de menor id.
#   2. Repunta active_storage_attachments de los duplicados al canónico.
#   3. Borra variant_records y filas de blobs duplicados.
#   4. Verifica que no queden attachments huérfanos (si quedan → ROLLBACK).
#   5. Exporta el mapa dup_key→keep_key para el borrado físico posterior.
#
# El borrado físico (modo files) es un paso SEPARADO y posterior: borra cada
# archivo duplicado SOLO si el archivo canónico existe en disco.
#
# Restaurar el backup si hiciera falta:
#   docker exec -i $(docker ps -q -f name=pgvector_pgvector) \
#     pg_restore -U postgres -d chatwoot_production --clean --if-exists \
#     < /root/chatwoot_backup_FECHA.dump
# ============================================================================
set -euo pipefail

DB="chatwoot_production"
PG_FILTER="pgvector_pgvector"
VOL="/var/lib/docker/volumes/chatwoot_v410_chatwoot_storage/_data"

CID=$(docker ps -q -f "name=${PG_FILTER}")
if [ -z "$CID" ]; then echo "ERROR: no encuentro el container de Postgres (${PG_FILTER})"; exit 1; fi

MODE="${1:-}"

case "$MODE" in
# ----------------------------------------------------------------------------
backup)
  OUT="/root/chatwoot_backup_$(date +%F_%H%M).dump"
  docker exec "$CID" pg_dump -U postgres -Fc "$DB" > "$OUT"
  ls -lh "$OUT"
  echo "Backup OK."
  ;;
# ----------------------------------------------------------------------------
test|full)
  if [ "$MODE" = "test" ]; then
    TC="ORDER BY count(*) ASC, min(id) ASC LIMIT 1"
  else
    TC=""
  fi
  docker exec -i "$CID" psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -P pager=off -v testclause="$TC" <<'SQL'
BEGIN;

CREATE TEMP TABLE canon AS
SELECT checksum, byte_size, min(id) AS keep_id
FROM active_storage_blobs
GROUP BY checksum, byte_size
HAVING count(*) > 1
:testclause;

CREATE TEMP TABLE dups AS
SELECT b.id AS dup_id, c.keep_id, b.key AS dup_key
FROM active_storage_blobs b
JOIN canon c USING (checksum, byte_size)
WHERE b.id <> c.keep_id;

CREATE TEMP TABLE filemap AS
SELECT d.dup_key, kb.key AS keep_key
FROM dups d JOIN active_storage_blobs kb ON kb.id = d.keep_id;

\echo == ALCANCE ==
SELECT (SELECT count(*) FROM canon) AS grupos,
       (SELECT count(*) FROM dups)  AS blobs_a_remover,
       pg_size_pretty((SELECT COALESCE(sum(b.byte_size),0) FROM active_storage_blobs b JOIN dups d ON d.dup_id=b.id)::bigint) AS espacio_a_liberar;

\echo == REPUNTANDO ATTACHMENTS ==
UPDATE active_storage_attachments a
SET blob_id = d.keep_id
FROM dups d
WHERE a.blob_id = d.dup_id;

\echo == BORRANDO VARIANTS Y BLOBS DUPLICADOS ==
DELETE FROM active_storage_variant_records WHERE blob_id IN (SELECT dup_id FROM dups);
DELETE FROM active_storage_blobs WHERE id IN (SELECT dup_id FROM dups);

DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n
  FROM active_storage_attachments a
  LEFT JOIN active_storage_blobs b ON b.id = a.blob_id
  WHERE b.id IS NULL;
  IF n > 0 THEN
    RAISE EXCEPTION 'ABORTADO: % attachments quedarían huérfanos', n;
  END IF;
END $$;

SELECT 'OK: cero attachments huérfanos' AS verificacion;

\copy (SELECT dup_key || ' ' || keep_key FROM filemap) TO '/tmp/dedup_files.txt'

COMMIT;
SQL
  docker cp "$CID":/tmp/dedup_files.txt "/root/dedup_files_${MODE}.txt"
  echo "Mapa de archivos: /root/dedup_files_${MODE}.txt ($(wc -l < /root/dedup_files_${MODE}.txt) líneas)"
  echo "Base deduplicada (${MODE}). Los archivos físicos siguen intactos hasta correr: bash dedup.sh files ${MODE}"
  ;;
# ----------------------------------------------------------------------------
files)
  SUB="${2:?Uso: bash dedup.sh files test|full}"
  MAPF="/root/dedup_files_${SUB}.txt"
  if [ ! -f "$MAPF" ]; then echo "ERROR: no existe $MAPF (corré antes: bash dedup.sh ${SUB})"; exit 1; fi
  echo "Disco ANTES:"; df -h / | tail -1
  BORRADOS=0; SALTADOS=0
  while read -r DUP KEEP; do
    [[ "$DUP"  =~ ^[A-Za-z0-9]+$ ]] || continue
    [[ "$KEEP" =~ ^[A-Za-z0-9]+$ ]] || continue
    KP="$VOL/${KEEP:0:2}/${KEEP:2:2}/$KEEP"
    DP="$VOL/${DUP:0:2}/${DUP:2:2}/$DUP"
    if [ -f "$KP" ]; then
      if [ -f "$DP" ]; then rm -f -- "$DP"; BORRADOS=$((BORRADOS+1)); fi
    else
      echo "SKIP (canónico ausente, no borro): $DUP"
      SALTADOS=$((SALTADOS+1))
    fi
  done < "$MAPF"
  echo "Archivos borrados: $BORRADOS | Saltados por seguridad: $SALTADOS"
  echo "Disco DESPUÉS:"; df -h / | tail -1
  ;;
# ----------------------------------------------------------------------------
status)
  docker exec "$CID" psql -U postgres -d "$DB" -P pager=off -c \
    "SELECT count(*) AS grupos_duplicados_restantes, pg_size_pretty(COALESCE(sum(esp),0)::bigint) AS espacio_duplicado FROM (SELECT sum(byte_size)*(count(*)-1)/count(*) AS esp FROM active_storage_blobs GROUP BY checksum, byte_size HAVING count(*)>1) t;"
  df -h / | tail -1
  ;;
# ----------------------------------------------------------------------------
*)
  echo "Uso: bash dedup.sh {backup|test|full|files test|files full|status}"
  exit 1
  ;;
esac
