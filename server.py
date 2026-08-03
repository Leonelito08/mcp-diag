"""
MCP de diagnóstico READ-ONLY para el server Hetzner (Chatwoot).

Diseño de seguridad (no negociable):
  1. Cada tool ejecuta comandos FIJOS como lista argv (nunca shell=True,
     nunca texto del modelo interpolado en un comando).
  2. Los únicos parámetros aceptados son números acotados o nombres
     validados contra allowlist + regex.
  3. Todo tiene timeout y la salida se trunca.
  4. El endpoint vive detrás de un path secreto (la URL es la credencial)
     y el proceso escucha solo en 127.0.0.1 — expuesto vía Cloudflare
     Tunnel o Caddy con HTTPS.

Extender = escribir una función con argv fijo + @mcp.tool() y reiniciar
el servicio. Ver README, sección "Cómo agregar herramientas".
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
CHATWOOT_LOCAL_URL = os.environ.get("CHATWOOT_LOCAL_URL", "http://127.0.0.1:3000")
REDIS_CONTAINER = os.environ.get("REDIS_CONTAINER", "").strip()
ALLOWED_CONTAINERS = {
    c.strip() for c in os.environ.get("ALLOWED_CONTAINERS", "").split(",") if c.strip()
}

MAX_OUTPUT_CHARS = 14_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")  # nombres docker/redis válidos

mcp = FastMCP("Hetzner Chatwoot — diagnóstico read-only")


# ----------------------------------------------------------------------
# Helper único de ejecución
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


# ----------------------------------------------------------------------
# Tools
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
    """Eventos del OOM-killer en el log del kernel (journalctl -k).
    Si aparece 'Killed process', el kernel estuvo matando procesos por falta de RAM."""
    lineas = _clamp(lineas, 1, 200, 40)
    out = run(["journalctl", "-k", "--no-pager", "-n", "5000"], timeout=25)
    claves = ("out of memory", "oom", "killed process")
    hits = [l for l in out.splitlines() if any(k in l.lower() for k in claves)]
    if not hits:
        return "Sin eventos OOM en las últimas 5000 líneas del log de kernel."
    return "\n".join(hits[-lineas:])


@mcp.tool()
def docker_status() -> str:
    """Estado de todos los containers (incl. detenidos) + consumo actual de CPU/RAM."""
    ps = run(
        ["docker", "ps", "-a", "--format",
         "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.Image}}"],
        timeout=15,
    )
    stats = run(
        ["docker", "stats", "--no-stream", "--format",
         "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}"],
        timeout=30,
    )
    return f"== docker ps -a ==\n{ps}\n\n== docker stats ==\n{stats}"


@mcp.tool()
def docker_disk() -> str:
    """Uso de disco de Docker: imágenes, containers, volúmenes y build cache."""
    return run(["docker", "system", "df"], timeout=30)


@mcp.tool()
def container_logs(container: str, lineas: int = 100) -> str:
    """Últimas N líneas de log de un container de la allowlist (ALLOWED_CONTAINERS en .env)."""
    if not ALLOWED_CONTAINERS:
        return ("ALLOWED_CONTAINERS está vacío en .env. "
                "Corré docker_status para ver los nombres reales y agregalos.")
    if container not in ALLOWED_CONTAINERS or not NAME_RE.match(container):
        return f"Container fuera de la allowlist. Permitidos: {sorted(ALLOWED_CONTAINERS)}"
    lineas = _clamp(lineas, 1, 500, 100)
    return run(["docker", "logs", "--tail", str(lineas), container], timeout=25)


@mcp.tool()
def sidekiq_queues() -> str:
    """Tamaño de las colas Sidekiq en Redis (pendientes por cola + retry/schedule/dead).
    Si Sidekiq murió, acá se ven colas creciendo — y Chatwoot deja de despachar webhooks."""
    if not REDIS_CONTAINER:
        return "Configurá REDIS_CONTAINER en .env (el nombre del container de Redis)."
    if not NAME_RE.match(REDIS_CONTAINER):
        return "REDIS_CONTAINER tiene un nombre inválido."
    base = ["docker", "exec", REDIS_CONTAINER, "redis-cli"]

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
    """Chequea que Chatwoot responda EN LOCAL (sin pasar por Cloudflare/DNS).
    Si esto responde 200 pero el sitio público no, el problema está afuera del server."""
    return run(
        ["curl", "-s", "-S", "-o", "/dev/null", "-m", "10",
         "-w", "HTTP %{http_code} en %{time_total}s (conexión %{time_connect}s)",
         CHATWOOT_LOCAL_URL],
        timeout=15,
    )


# ----------------------------------------------------------------------
# Arranque
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if len(SECRET) < 32:
        raise SystemExit(
            "MCP_SECRET faltante o demasiado corto (mínimo 32 caracteres). "
            "Generalo con: openssl rand -hex 32"
        )
    # Escucha SOLO en localhost; lo expone el túnel/proxy con HTTPS.
    # La URL final del conector es: https://<tu-hostname>/<MCP_SECRET>/mcp
    mcp.run(transport="http", host="127.0.0.1", port=PORT, path=f"/{SECRET}/mcp")
