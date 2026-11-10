# Private research VPS preparation

This Compose file runs the private research installation. It
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

A push to `main` starts deployment in the affected repository. GitHub runs
its CI checks, publishes the checked image, and invokes the VPS release handler
over restricted SSH. The Actions page also supports manual runs. If the other
StockBOT deployment is holding the VPS lock, GitHub retries for up to 15 minutes.
Keep frontend and backend versions compatible. The backend
deployment stops new API requests, waits up to 600 seconds for its worker,
applies migrations, synchronizes the registry and verifies readiness. A failed
migration can leave the API and worker stopped and requires operator attention;
the release does not automatically downgrade the database. No off-VPS backup
is configured yet. CI success is not evidence that research results are
correct: the identified look-ahead issue remains unresolved pending partner
clarification and implementation. Live trading is outside scope.
