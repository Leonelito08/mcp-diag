# MCP de diagnóstico read-only — Hetzner (Chatwoot)

Servicio chico que corre en el propio server y expone herramientas de
diagnóstico **de solo lectura** vía MCP, para que Claude (claude.ai) pueda
revisar el estado del Hetzner igual que hoy revisa Railway y Neon.

**Qué puede hacer Claude con esto:** ver RAM/disco/uptime, detectar eventos
OOM, estado y consumo de containers Docker, leer logs de containers de una
allowlist, medir colas de Sidekiq en Redis y chequear que Chatwoot responda
en local.

**Qué NO puede hacer:** ejecutar comandos arbitrarios, escribir, reiniciar
nada, leer archivos. Cada herramienta es un comando fijo con timeout.

---

## Instalación (10-15 min)

Todo como root salvo indicación contraria.

### 1. Bajar el código y crear el usuario

```bash
apt update && apt install -y git python3-venv curl
git clone https://github.com/Leonelito08/mcp-diag.git /opt/mcp-diag
useradd -r -s /usr/sbin/nologin -d /opt/mcp-diag mcpdiag
cd /opt/mcp-diag
```

(Actualizaciones futuras: `cd /opt/mcp-diag && git pull && systemctl restart mcp-diag`.)

### 2. Entorno Python

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. Configuración

```bash
cp env.example .env
openssl rand -hex 32        # pegar el resultado en MCP_SECRET dentro de .env
docker ps --format '{{.Names}}'   # con esto completás ALLOWED_CONTAINERS y REDIS_CONTAINER
nano .env
chown -R mcpdiag:mcpdiag /opt/mcp-diag
chmod 600 .env
```

`CHATWOOT_LOCAL_URL`: el puerto donde rails escucha en el host
(verificalo con `docker ps` — columna de puertos, típicamente `3000`).

### 4. Servicio systemd

```bash
cp mcp-diag.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mcp-diag
systemctl status mcp-diag          # debe decir "active (running)"
```

Prueba local (debe responder algo, aunque sea un error de protocolo MCP —
lo importante es que NO sea "connection refused"):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8321/$(grep MCP_SECRET .env | cut -d= -f2)/mcp
```

### 5. Exponerlo con HTTPS

El servicio escucha solo en `127.0.0.1`. Elegí UNA de las dos opciones.

**Opción A — Cloudflare Tunnel (recomendada: cero puertos abiertos)**

Requiere un dominio tuyo administrado en Cloudflare (sirve un subdominio de
cualquiera que ya tengas ahí).

```bash
# Instalar cloudflared
curl -L -o /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x /usr/local/bin/cloudflared

cloudflared tunnel login                 # abre URL, elegís el dominio
cloudflared tunnel create mcp-diag
cloudflared tunnel route dns mcp-diag mcp.TUDOMINIO.com
```

Crear `/etc/cloudflared/config.yml` (el ID del túnel te lo dio `create`):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: mcp.TUDOMINIO.com
    service: http://127.0.0.1:8321
  - service: http_status:404
```

```bash
cloudflared service install
systemctl enable --now cloudflared
```

**Opción B — Caddy (si preferís no depender de Cloudflare)**

Necesita un registro DNS A → IP del Hetzner y el puerto 443 abierto.

```bash
apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
mcp.TUDOMINIO.com {
    reverse_proxy 127.0.0.1:8321
}
```

```bash
systemctl reload caddy
```

Caddy gestiona el certificado Let's Encrypt solo.

### 6. Agregar el conector en claude.ai

1. En claude.ai: **Configuración → Conectores → "+" → Agregar conector personalizado**.
2. Nombre: `Hetzner Chatwoot`.
3. URL: `https://mcp.TUDOMINIO.com/<MCP_SECRET>/mcp`
   (el secreto va en la URL — esa URL ES la credencial, no la compartas).
4. Guardar. No hace falta OAuth.

### 7. Probar

En una conversación nueva: *"Revisá el estado del Hetzner"*. Claude debería
poder llamar `system_stats`, `docker_status`, `oom_check`, etc.

---

## Cómo agregar herramientas (para futuras necesidades de monitoreo)

Receta: función con **argv fijo** + decorador + reiniciar. Ejemplo — chequear
que Postgres acepte conexiones:

```python
@mcp.tool()
def postgres_ping() -> str:
    """pg_isready dentro del container de Postgres."""
    return run(["docker", "exec", "chatwoot-postgres-1", "pg_isready"], timeout=10)
```

```bash
systemctl restart mcp-diag
```

Reglas al extender:
- Solo lectura. Nada de restart/exec arbitrario/escritura, aunque tiente.
- Nunca interpolar strings del modelo en comandos: parámetros solo como
  números acotados o nombres validados contra allowlist (mirá
  `container_logs` como plantilla).
- Siempre `timeout=`.

El mismo patrón sirve para cualquier server futuro: copiás la carpeta,
cambiás las tools, otro subdominio, otro secreto.

## Operación

- **Rotar credencial**: nuevo `openssl rand -hex 32` en `.env` →
  `systemctl restart mcp-diag` → actualizar la URL del conector en claude.ai.
- **Logs del servicio**: `journalctl -u mcp-diag -f`
- **Actualizar fastmcp**: `./venv/bin/pip install -U "fastmcp>=2.10,<3"` y restart.
- Si algún día preferís credencial en header en vez de URL: claude.ai tiene
  soporte (en beta) de request headers para conectores; el cambio en el
  server es chico — pedírselo a Claude cuando toque.
