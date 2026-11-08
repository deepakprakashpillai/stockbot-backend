# Private research VPS preparation

This Compose file is a preparation artifact, not a completed deployment. It
keeps the API, worker, frontend, PostgreSQL, Redis, and market data separate
from other applications. Only the API and frontend bind to loopback; an HTTPS
proxy must route `/api/` (including WebSockets) to port 18080 and the website
to port 18081 on a dedicated domain. Do not expose database or Redis ports.

Before use, provide a private environment file outside Git with
`BACKEND_IMAGE`, `FRONTEND_IMAGE`, `STOCKBOT_DOMAIN`, `POSTGRES_PASSWORD`, and
`STOCKBOT_LOCAL_ACCESS_KEY`. Use verified compatible image digests, a unique
random hexadecimal database password (so it is safe in the URL), and a unique
random access key of at least 32 characters. Protect the file with mode 600.
Configure TLS, registry access, matched off-VPS backups and tested restoration.
Apply migrations and sync the algorithm registry before starting traffic; keep
workers stopped during migration. Verify readiness, authenticated browser and
WebSocket access, queue recovery, dataset availability and limitations, and
record release digests. Do not overwrite another application's proxy or ports.

There is no automated VPS deployment workflow yet. It must be built around the
actual server, protected access, compatible frontend/backend versions and
backup/rollback process. CI success is not evidence that research results are
correct: the identified look-ahead issue remains unresolved pending partner
clarification and implementation. Live trading is outside scope.
