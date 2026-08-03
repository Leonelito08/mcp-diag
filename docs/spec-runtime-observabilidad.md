# Spec — Observabilidad del agent-runtime (para ejecutar con Claude Code)

Repo: `Leonelito08/experienciadeviajes-agent-runtime` (Python/FastAPI, deploy en Railway).
Contexto: los cron de Railway (alpine/curl) pegan a endpoints del runtime; el de
transcripciones procesa sincrónico y cuando hay trabajo real supera los 100s de
Cloudflare → 524 → el cron sale con error → mails de "crashed" 2-3 veces por día.
Además el runtime no tiene error tracking ni health profundo.

## Entregable 1 — Fix del 524 (endpoint de transcripciones asíncrono)
- El endpoint que dispara el procesamiento de transcripciones debe responder
  `202 Accepted` de inmediato y procesar en background (`BackgroundTasks` de
  FastAPI o `asyncio.create_task`).
- Agregar lock simple (asyncio.Lock o flag en memoria) para que si el cron pega
  cada 5 min y el batch anterior sigue corriendo, NO se lance un segundo batch:
  responder `202` con `{"status": "already_running"}`.
- Revisar que el procesamiento no tenga llamadas bloqueantes (sin await /
  CPU-bound) que congelen el event loop mientras corre — si las hay, moverlas a
  `run_in_executor` o thread.
- Aceptación: curl al endpoint devuelve 202 en < 1s siempre; los logs muestran
  el batch completándose en background; desaparecen los 524 en los cron.

## Entregable 2 — GET /health profundo
- Nuevo endpoint `GET /health` que verifique con timeout corto (3-5s c/u):
  1. DB Neon: `SELECT 1`.
  2. Chatwoot API alcanzable (GET liviano con el admin token, ej. profile).
  3. Config crítica presente (ANTHROPIC_API_KEY definido — no validar contra la
     API en cada hit, solo presencia).
- Respuesta: `200 {"ok": true, "checks": {...}}` si todo bien;
  `503 {"ok": false, "checks": {...}}` si algo falla.
- Sin auth (es el target del monitor externo de uptime), sin datos sensibles en
  la respuesta.
- Aceptación: con Neon o Chatwoot caídos (simular con env inválida en local),
  devuelve 503 con el check correspondiente en false.

## Entregable 3 — Sentry
- `sentry-sdk[fastapi]` inicializado solo si existe env `SENTRY_DSN`.
- `traces_sample_rate=0` (solo errores), `send_default_pii=False`.
- Verificar que las excepciones en background tasks también se capturen.
- Aceptación: endpoint temporal de prueba (o script) que lance una excepción y
  aparezca en el proyecto de Sentry; luego remover el endpoint de prueba.

## Notas
- No tocar la lógica de negocio de Manu ni el pre-check fail-closed.
- Deploy: push a main → Railway (flujo habitual del repo).
- Al terminar, avisar para apuntar el monitor externo de uptime a `/health`.
