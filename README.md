# Appointment Lifecycle Automation

An AI-assisted appointment lifecycle automation system: request intake →
AI interpretation → deterministic availability & booking → confirmation →
reminders → completion / cancellation / no-show → recovery & rebooking.

The guiding principle throughout: **AI for interpretation, deterministic
systems for control.** AI never decides availability, prevents double
bookings, or authorizes a booking on its own — it turns language into a
structured, validated request that deterministic code then checks against
real business rules and real database state.

## Status

Architecture proposed and reviewed; implementation in progress.

- [x] **Phase 0** — repo, tooling, Docker Compose, CI, security baseline
- [ ] Phase 1 — domain core: schema, deterministic availability engine, atomic booking
- [ ] Phase 2 — AI interpretation layer + golden-set evaluation
- [ ] Phase 3 — workflow engine, audit trail, notification interface
- [ ] Phase 4 — calendar provider abstraction (mock + Google Calendar)
- [ ] Phase 5 — background jobs: reminders, no-show detection, recovery
- [ ] Phase 6 — cancellation & rescheduling
- [ ] Phase 7 — real notification provider
- [ ] Phase 8 — dashboard
- [ ] Phase 9 — security hardening, full evaluation suite, deployment

## Stack

FastAPI · PostgreSQL · SQLAlchemy (async) · Alembic · Docker Compose ·
GitHub Actions · Anthropic Claude (Phase 2+) · Next.js (Phase 8+)

## Local development

```bash
cp .env.example .env   # already done if you cloned this repo fresh — fill in real values later
docker compose up --build
```

The API comes up at `http://localhost:8010` (mapped off the default 8000/5432
to avoid clashing with other local projects — see `docker-compose.yml`);
`GET /health` checks both the process and the database connection. Postgres
itself is reachable from the host at `localhost:5436` if you need to inspect
it directly (e.g. with `psql`).

Running tests and linters directly:

```bash
cd backend
pip install -r requirements-dev.txt
pytest
ruff check .
black --check .
```

## Security

- `.env` is gitignored; only `.env.example` (placeholders only) is committed.
- Secret scanning runs in CI (`gitleaks`) and locally via `pre-commit`:
  ```bash
  pip install pre-commit
  pre-commit install
  ```
- No real credentials belong in code, tests, seed data, or documentation —
  see `.env.example` for every credential the system uses and where it's
  introduced in the phase plan.
