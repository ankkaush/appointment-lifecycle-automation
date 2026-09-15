# Deployment

This covers running the system in production shape. It does not deploy
to any specific host -- see [Real cloud infrastructure](#real-cloud-infrastructure-not-included)
below for what a real deployment would still need beyond this repo.

## What "production shape" means here

`docker-compose.yml` is the local-dev stack: `--reload`, source bind-mounted
into each container, hardcoded `changeme` Postgres credentials.
`docker-compose.prod.yml` is the same four services (`db`, `api`, `worker`,
`dashboard`) built and run differently:

- `backend/Dockerfile.prod` -- no `--reload`, no bind-mounted source, runs
  as a non-root user, multiple uvicorn workers.
- `frontend/Dockerfile.prod` -- `next build` + `next start` via Next.js's
  standalone output, not `next dev`, also non-root.
- No host port published for `db` -- only the `api`/`worker` containers
  can reach it, over the compose network.
- Every credential comes from the environment (`.env`, or your platform's
  secret store) instead of a hardcoded default.

## Before starting

Copy `.env.example` to `.env` and fill in real values for production:

- `SECRET_KEY` -- a real random value, not the dev default.
- `POSTGRES_PASSWORD` -- required, no default (the prod compose file
  refuses to start without it).
- `API_PUBLIC_URL` -- the API's real public URL (e.g.
  `https://api.yourdomain.com`). Baked into the dashboard's browser
  bundle at *build* time (see `frontend/Dockerfile.prod`'s comment on
  why this can't be a runtime container env var like the others).
- `DASHBOARD_ORIGIN` -- the dashboard's real public origin, for CORS.
- `ANTHROPIC_API_KEY` -- required in production (the app won't start
  without it -- see below).
- `NOTIFICATION_PROVIDER=resend` plus `RESEND_API_KEY` /
  `RESEND_FROM_EMAIL`, if you want real email delivery rather than the
  `console` default (which only logs).
- `APP_ENV=production` -- this is what turns on the startup check below.
  Leaving it unset (or `development`) skips the check entirely, which is
  correct for local dev but means a real deployment that forgets this
  line gets none of the protection it's meant to provide.

### The production startup check

`app/core/startup_checks.py` runs once, at process startup, only when
`APP_ENV=production`. It refuses to boot -- not "logs a warning," refuses
to start -- if `SECRET_KEY` or `DASHBOARD_ORIGIN` are still their local-dev
defaults, if `DATABASE_URL` still contains the dev placeholder password,
if `ANTHROPIC_API_KEY` is unset, or if `NOTIFICATION_PROVIDER=resend`
without both Resend credentials. The failure happens at `docker run`
time, with every problem listed at once, rather than surfacing later as
a confusing runtime error on the first real request.

## Running it

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

Then run migrations as a separate, explicit step (deliberately not run
automatically on container start -- see `backend/Dockerfile.prod`'s
comment on why racing multiple replicas through a boot-time migration is
worth avoiding):

```bash
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
```

Check `GET /health` (proves the process is up *and* the database
connection is real) before pointing real traffic at it.

## Real cloud infrastructure not included

This repo gets you a production-shaped set of images and a compose file
that runs them correctly together. It does not include:

- An actual host to run them on, or the account/billing setup for one.
- TLS termination / a reverse proxy in front of `api`/`dashboard` (put
  one in front for real use -- e.g. Caddy, nginx, or your platform's
  load balancer).
- A managed Postgres instance -- `docker-compose.prod.yml` runs Postgres
  as a container, which is a reasonable self-contained option, but
  pointing `DATABASE_URL` at a managed instance instead and dropping the
  `db` service is equally valid and often preferable for real
  production use (backups, failover, connection pooling).
- Log aggregation, metrics, or alerting.
- CI/CD wiring to actually build and push these images to a registry on
  push to `main` (the existing `.github/workflows/ci.yml` builds and
  tests every push; it does not deploy).

None of this is hard to add on top -- it's just genuinely a different
decision (which host, which registry, which proxy) that depends on where
you're actually deploying, not something this repo should assume for you.
