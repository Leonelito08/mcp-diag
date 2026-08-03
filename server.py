"""
MCP de diagnóstico READ-ONLY para el server Hetzner (Chatwoot en Docker Swarm).
v1.3 — medición de storage: postgres_databases + chatwoot_storage_report
(consultas SELECT fijas, auditables acá; el modelo no puede enviar SQL propio).
v1.2 — modo stateless + respuestas JSON planas (compatibilidad claude.ai).
v1.1 — soporte Swarm: allowlist por SERVICIO, service logs, resolución de tasks.

Diseño de seguridad (no negociable):
  1. Cada tool ejecuta comandos FIJOS como lista argv (nunca shell=True,
     nunca texto del modelo interpolado en un comando o en SQL).
  2. Los únicos parámetros aceptados son números acotados o nombres
     validados contra allowlist + regex.
  3. Todo tiene timeout y la salida se trunca.
  4. El endpoint vive detrás de un path secreto (la URL es la credencial)
     y el proceso escucha solo en 127.0.0.1 — expuesto vía Cloudflare
     Tunnel con HTTPS.
"""

import os
import re
import subprocess

from fastmcp import FastMCP

# ----------------------------------------------------------------------
# Configuración (viene de /opt/mcp-diag/.env vía systemd EnvironmentFile)
# ----------------------------------------------------------------------
SECRET = os.environ.get("MCP_SECRET", "")
PORT = int(os.environ.get("MCP_PORT", "8321"))
CHATWOOT_LOCAL_URL = os.environ.get("CHATWOOT_LOCAL_URL", "http://127.0.0.1:80")
CHATWOOT_HOST_HEADER = os.environ.get("CHATWOOT_HOST_HEADER", "").strip()
REDIS_SERVICE = os.environ.get("REDIS_SERVICE", "").strip()
PG_SERVICE = os.environ.get("PG_SERVICE", "pgvector_pgvector").strip()
ALLOWED_SERVICES = {
    s.strip() for s in os.environ.get("ALLOWED_SERVICES", "").split(",") if s.strip()
}

MAX_OUTPUT_CHARS = 14_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
HEX_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

mcp = FastMCP("Hetzner Chatwoot — diagnóstico read-only")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def run(argv: list[str], timeout: int = 15) -> str:
    """Ejecuta un comando fijo (argv), con timeout y salida truncada."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT {timeout}s] {' '.join(argv)}"
    except FileNotFoundError:
        return f"[ERROR] comando no encontrado: {argv[0]}"

    out = p.stdout or ""
    if p.stderr.strip():
        out += f"\n[stderr]\n{p.stderr.strip()}"
    out = out.strip() or f"(sin salida, exit={p.returncode})"
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n[... truncado a {MAX_OUTPUT_CHARS} caracteres]"
    return out


def _clamp(n, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(n), hi))
    except (TypeError, ValueError):
        return default


def _resolve_task(service: str) -> str | None:
    """Container ID de la task corriendo de un servicio swarm (nombre ya validado)."""
    out = run(["docker", "ps", "-q", "--filter", f"name={service}"], timeout=10)
    for line in out.splitlines():
        if HEX_ID_RE.match(line.strip()):
            return line.strip()
    return None


def _psql(db: str, sql: str, timeout: int = 45) -> str:
    """Ejecuta una consulta FIJA (definida en este archivo) vía psql dentro del
    container de Postgres. `db` viene validado contra NAME_RE por el caller."""
    if not NAME_RE.match(PG_SERVICE):
        return "PG_SERVICE inválido en .env."
    cid = _resolve_task(PG_SERVICE)
    if not cid:
        return f"No hay task corriendo del servicio {PG_SERVICE}."
    return run(
        ["docker", "exec", cid, "psql", "-U", "postgres", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "-c", sql],
        timeout=timeout,
    )


# ----------------------------------------------------------------------
# Tools — sistema y docker
# ----------------------------------------------------------------------
@mcp.tool()
def system_stats() -> str:
    """RAM, swap, disco, uptime y load del server (free -h, df -h, uptime)."""
    secciones = [
        ("== free -h ==", ["free", "-h"], 10),
        ("== df -h ==", ["df", "-h", "-x", "tmpfs", "-x", "overlay"], 10),
        ("== uptime ==", ["uptime"], 5),
    ]
    return "\n\n".join(f"{t}\n{run(cmd, timeout=to)}" for t, cmd, to in secciones)


@mcp.tool()
def top_mem() -> str:
    """Top 12 procesos por consumo de memoria."""
    out = run(["ps", "aux", "--sort=-%mem"], timeout=10)
    return "\n".join(out.splitlines()[:13])


@mcp.tool()
def oom_check(lineas: int = 40) -> str:
    """Eventos del OOM-killer en el log del kernel (journalctl -k, boot actual)."""
    lineas = _clamp(lineas, 1, 200, 40)
    out = run(["journalctl", "-k", "--no-pager", "-n", "5000"], timeout=25)
    claves = ("out of memory", "oom", "killed process")
    hits = [l for l in out.splitlines() if any(k in l.lower() for k in claves)]
    if not hits:
        return "Sin eventos OOM en las últimas 5000 líneas del log de kernel."
    return "\n".join(hits[-lineas:])


@mcp.tool()
def docker_status() -> str:
    """Servicios swarm con réplicas (0/1 = caído), containers y consumo."""
    svc = run(
        ["docker", "service", "ls", "--format",
         "table {{.Name}}\t{{.Replicas}}\t{{.Image}}"],
        timeout=15,
    )
    ps = run(
        ["docker", "ps", "-a", "--format",
         "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"],
        timeout=15,
    )
    stats = run(
        ["docker", "stats", "--no-stream", "--format",
         "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}"],
        timeout=30,
    )
    return (f"== docker service ls ==\n{svc}\n\n"
            f"== docker ps -a ==\n{ps}\n\n== docker stats ==\n{stats}")


@mcp.tool()
def docker_disk() -> str:
    """Uso de disco de Docker: imágenes, containers, volúmenes y build cache."""
    return run(["docker", "system", "df"], timeout=30)


@mcp.tool()
def service_logs(service: str, lineas: int = 100) -> str:
    """Últimas N líneas de log de un servicio swarm de la allowlist."""
    if not ALLOWED_SERVICES:
        return "ALLOWED_SERVICES está vacío en .env."
    if service not in ALLOWED_SERVICES or not NAME_RE.match(service):
        return f"Servicio fuera de la allowlist. Permitidos: {sorted(ALLOWED_SERVICES)}"
    lineas = _clamp(lineas, 1, 500, 100)
    return run(
        ["docker", "service", "logs", "--no-task-ids", "--tail", str(lineas), service],
        timeout=30,
    )


@mcp.tool()
def sidekiq_queues() -> str:
    """Colas Sidekiq en Redis (pendientes por cola + retry/schedule/dead)."""
    if not REDIS_SERVICE:
        return "Configurá REDIS_SERVICE en .env."
    if not NAME_RE.match(REDIS_SERVICE):
        return "REDIS_SERVICE tiene un nombre inválido."
    cid = _resolve_task(REDIS_SERVICE)
    if not cid:
        return f"No hay ninguna task corriendo del servicio {REDIS_SERVICE} — ¿Redis caído?"
    base = ["docker", "exec", cid, "redis-cli"]

    ping = run(base + ["ping"], timeout=10)
    if "PONG" not in ping:
        return f"Redis no responde: {ping}"

    lineas = [f"redis ping: {ping}"]
    colas = run(base + ["smembers", "queues"], timeout=10).split()
    for q in colas:
        if NAME_RE.match(q):
            lineas.append(f"queue:{q} = {run(base + ['llen', f'queue:{q}'], timeout=10)}")
    for zset in ("retry", "schedule", "dead"):
        lineas.append(f"{zset} = {run(base + ['zcard', zset], timeout=10)}")
    return "\n".join(lineas)


@mcp.tool()
def chatwoot_health() -> str:
    """Cadena Traefik → Chatwoot desde el propio server (sin salir a internet)."""
    argv = ["curl", "-s", "-S", "-o", "/dev/null", "-m", "10",
            "-w", "HTTP %{http_code} en %{time_total}s (conexión %{time_connect}s)"]
    if CHATWOOT_HOST_HEADER:
        argv += ["-H", f"Host: {CHATWOOT_HOST_HEADER}"]
    argv += [CHATWOOT_LOCAL_URL]
    return run(argv, timeout=15)


# ----------------------------------------------------------------------
# Tools — medición de storage (v1.3, consultas SELECT fijas)
# ----------------------------------------------------------------------
@mcp.tool()
def postgres_databases() -> str:
    """Bases de datos del Postgres (servicio PG_SERVICE) con su tamaño."""
    return _psql(
        "postgres",
        "SELECT datname AS base, pg_size_pretty(pg_database_size(datname)) AS tamaño "
        "FROM pg_database WHERE NOT datistemplate "
        "ORDER BY pg_database_size(datname) DESC;",
    )


@mcp.tool()
def chatwoot_storage_report(db: str) -> str:
    """Medición READ-ONLY del storage de Chatwoot en la base indicada:
    tamaño por tipo de registro, top duplicados, GB recuperables por dedup
    (total y solo-enviados), y crecimiento mensual orgánico vs total.
    Todas las consultas son SELECT fijas definidas en este archivo."""
    if not NAME_RE.match(db):
        return "Nombre de base inválido."

    q_tipos = (
        "SELECT att.record_type AS tipo, count(*) AS archivos, "
        "pg_size_pretty(sum(b.byte_size)::bigint) AS total "
        "FROM active_storage_blobs b "
        "JOIN active_storage_attachments att ON att.blob_id = b.id "
        "GROUP BY 1 ORDER BY sum(b.byte_size) DESC;"
    )
    q_top_dup = (
        "SELECT min(b.filename) AS ejemplo, pg_size_pretty(b.byte_size::bigint) AS tam_unit, "
        "count(*) AS copias, pg_size_pretty((b.byte_size * count(*))::bigint) AS ocupa "
        "FROM active_storage_blobs b "
        "GROUP BY b.checksum, b.byte_size HAVING count(*) > 1 "
        "ORDER BY b.byte_size * count(*) DESC LIMIT 15;"
    )
    q_dedup_total = (
        "SELECT pg_size_pretty(COALESCE(sum(byte_size * (cnt - 1)), 0)::bigint) AS recuperable_dedup "
        "FROM (SELECT byte_size, count(*) AS cnt FROM active_storage_blobs "
        "GROUP BY checksum, byte_size HAVING count(*) > 1) t;"
    )
    q_env_rec = (
        "SELECT CASE m.message_type WHEN 0 THEN 'recibido' WHEN 1 THEN 'enviado' "
        "ELSE 'otro' END AS direccion, count(*) AS archivos, "
        "pg_size_pretty(sum(b.byte_size)::bigint) AS total "
        "FROM active_storage_blobs b "
        "JOIN active_storage_attachments att ON att.blob_id = b.id AND att.record_type = 'Attachment' "
        "JOIN attachments a ON a.id = att.record_id "
        "JOIN messages m ON m.id = a.message_id "
        "GROUP BY 1 ORDER BY sum(b.byte_size) DESC;"
    )
    q_dedup_enviados = (
        "SELECT pg_size_pretty(COALESCE(sum(t.byte_size * (t.cnt - 1)), 0)::bigint) AS recuperable_dedup_enviados "
        "FROM (SELECT b.checksum, b.byte_size, count(*) AS cnt "
        "FROM active_storage_blobs b "
        "JOIN active_storage_attachments att ON att.blob_id = b.id AND att.record_type = 'Attachment' "
        "JOIN attachments a ON a.id = att.record_id "
        "JOIN messages m ON m.id = a.message_id AND m.message_type = 1 "
        "GROUP BY 1, 2 HAVING count(*) > 1) t;"
    )
    q_mensual = (
        "WITH dup AS (SELECT checksum FROM active_storage_blobs "
        "GROUP BY checksum HAVING count(*) > 1) "
        "SELECT to_char(b.created_at, 'YYYY-MM') AS mes, "
        "pg_size_pretty(COALESCE(sum(b.byte_size) FILTER "
        "(WHERE b.checksum NOT IN (SELECT checksum FROM dup)), 0)::bigint) AS organico, "
        "pg_size_pretty(sum(b.byte_size)::bigint) AS total "
        "FROM active_storage_blobs b "
        "GROUP BY 1 ORDER BY 1 DESC LIMIT 12;"
    )

    secciones = [
        ("== Tamaño por tipo de registro ==", q_tipos),
        ("== Top 15 archivos duplicados (por espacio ocupado) ==", q_top_dup),
        ("== Recuperable con dedup (todo) ==", q_dedup_total),
        ("== Enviado vs recibido (adjuntos de mensajes) ==", q_env_rec),
        ("== Recuperable con dedup SOLO enviados ==", q_dedup_enviados),
        ("== Crecimiento mensual: orgánico (únicos) vs total ==", q_mensual),
    ]
    return "\n\n".join(f"{t}\n{_psql(db, q)}" for t, q in secciones)


# ----------------------------------------------------------------------
# Arranque
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if len(SECRET) < 32:
        raise SystemExit(
            "MCP_SECRET faltante o demasiado corto (mínimo 32 caracteres). "
            "Generalo con: openssl rand -hex 32"
        )
    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=PORT,
        path=f"/{SECRET}/mcp",
        stateless_http=True,
        json_response=True,
    )
