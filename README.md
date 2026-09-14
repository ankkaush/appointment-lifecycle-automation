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

- [x] Phase 0 — repo, tooling, Docker Compose, CI, security baseline
- [x] Phase 1 — domain core: schema, deterministic availability engine, atomic booking
- [x] Phase 2 — AI interpretation layer + golden-set evaluation
- [x] Phase 3 — workflow engine, audit trail, notification interface
- [x] **Phase 4** — customer web chat (bounded clarification loop + thin chat UI)
- [ ] Phase 5 — calendar provider abstraction (mock + Google Calendar)
- [ ] Phase 6 — background jobs: reminders, no-show detection, recovery, expiring abandoned conversations
- [ ] Phase 7 — cancellation & rescheduling
- [ ] Phase 8 — real notification provider
- [ ] Phase 9 — business dashboard
- [ ] Phase 10 — security hardening, full evaluation suite, deployment

Phase 1 detail: `app/domain/` holds the business/service/staff/customer/
appointment schema, the deterministic availability engine (working hours,
blocked periods, buffer time, booking-notice/horizon policy), and the
booking engine. Concurrency safety comes from a Postgres range-exclusion
constraint on `appointments` plus a client-supplied idempotency key —
proven under a real concurrent-request race in
`tests/domain/test_concurrency.py`. `app/api/` is a thin HTTP layer over
that — no booking or availability logic lives there.

Phase 2 detail: `app/ai/` holds the interpretation layer — a provider-
agnostic `Interpreter` Protocol, structured schemas, and a deterministic
`resolve.py` that matches the AI's free-text service mention against a
business's real configured services (never a guess: unmatched returns
`None`). `app/ai/providers/claude.py` is the only module allowed to import
the Anthropic SDK; `app/ai/providers/fake.py` is a no-network stand-in
used by every test via FastAPI dependency override. Confidence is
advisory-only by design — never a gate on booking, per the architecture
baseline's Concern 4. `eval/` holds a golden set (deliberately including
ambiguous/hard cases, not just easy wins) and `run_eval.py`, a standalone
script — not part of `pytest` — that measures intent accuracy, ambiguity
recall, service-hint accuracy, and a false-confidence rate against the
real Anthropic API. Wired into CI as an opt-in job gated on an
`ANTHROPIC_API_KEY` secret and an `AI_EVAL_ENABLED` repo variable, so it's
skipped rather than failing CI for anyone without a key configured.

Verified live once against the real API (kept deliberately minimal): one
isolated smoke call through `ClaudeInterpreter`, then the golden set run
once — 95% intent accuracy, 100% ambiguity recall, 1/24 false-confidence.
`pytest` itself never calls the real API.

Phase 3 detail: `app/workflow/` is the orchestrator — the one module that
composes `app/domain` and `app/ai`, which stay independent of each other
and of it. Two state machines, per the architecture baseline's Concern 1
correction: `ProcessingRun` (`RECEIVED → INTERPRETING → SLOTS_OFFERED →
AWAITING_CONFIRMATION → BOOKING → SUCCEEDED/FAILED/ESCALATED/EXPIRED`,
enforced by a deterministic transition table in `state_machine.py`, not
just implied) and `Appointment` (unchanged from Phase 1). Every state
change is written to `AuditEvent` (the business record); every execution
step — including ones with no state change — to `WorkflowStep` (the
trace); every AI call's structured output and cost/latency to
`AIInvocation`. An ambiguous request, an unmatched service, or any
non-booking intent escalates cleanly to an `EscalationCase` rather than
guessing. If a confirmed slot is taken by a concurrent request between
offer and confirm, the workflow re-offers fresh availability instead of
just failing (`tests/workflow/test_orchestrator.py`, the re-offer race
test). `NotificationService` (Protocol + `MockNotificationProvider`) is
the vendor boundary for confirmations — a real provider arrives in Phase
8 as a new module, not new call sites. `GET /v1/requests/{id}/trace`
answers "what happened to this request?" directly from these tables — no
separate tracing stack.

Phase 4 detail: extends Phase 3's `ProcessingRun` state machine with one
new state, `AWAITING_CLARIFICATION` (`INTERPRETING` is the hub state,
reached fresh or via a clarification reply — the post-interpretation
routing decision always happens from the same place). Bounded, not an
open-ended chatbot: at most 2 clarification rounds
(`orchestrator.MAX_CLARIFICATION_ROUNDS`) before escalating rather than
looping — the same cap the original architecture proposal specified for
ambiguous requests. The AI's existing `ambiguity_reason` field doubles as
the clarifying question, so Phase 2's contract needed no changes. A
bounded transcript (`ProcessingRun.messages`, JSONB — operational data,
same shorter-retention register as `WorkflowStep`, not a permanent
record; no purge job yet, landing with Phase 6's sweep) carries context
across turns without a new conversation-domain concept. First-time
customer identification (`app/domain/customers.py`,
`find_or_create_customer`) resolves a name+contact into a `Customer` row
with no account system — any channel can use it, not just chat. The chat
UI itself (`static/chat/`, mounted at `/chat`) is a single static HTML
file with inline CSS/JS — no framework, no build step, and no
booking/availability logic; it only calls `POST /v1/requests`, `POST
/v1/requests/{id}/reply`, and `POST /v1/requests/{id}/confirm`. Slot
selection is button-based, not free-text-parsed — deterministic UI
selection where a language-understanding step isn't needed.
`app/api/rate_limit.py` adds a minimal in-memory per-IP limiter on the
two AI-invoking public endpoints (real production rate limiting is
Phase 10 hardening scope; this exists because Phase 4 is what first
exposes a public, unauthenticated, AI-calling endpoint).

Verified live against the real API (kept minimal, per the same budget
discipline as Phase 2): one full conversation exercised end to end
through the actual browser UI — an initial message correctly resolved to
a missing-service escalation, then a second conversation's ambiguous
"no date given" case correctly triggered a clarifying question, a reply
resolved it, real slots were offered, and a real booking was confirmed.
`pytest` itself never calls the real API — all 47 tests use
`FakeInterpreter` or small purpose-built stubs.

**Finding worth noting**: live testing surfaced that Phase 2's system
prompt frames ambiguity around missing/vague *dates*, not around
book-vs-question *intent* ambiguity — a message like "Can I come Friday?"
resolved confidently to a booking request with no service mentioned
(and correctly escalated for that reason) rather than being flagged as
possibly-a-question. Also, the AI's `ambiguity_reason` field, reused
directly as the clarifying question shown to the customer, reads like an
internal diagnostic note rather than natural chat copy. Both are prompt/
copy tuning opportunities for a follow-up, not defects in this phase's
mechanism, which is verified working correctly end to end.

## Stack

FastAPI · PostgreSQL · SQLAlchemy (async) · Alembic · Docker Compose ·
GitHub Actions · Anthropic Claude (Phase 2+) · Next.js (Phase 9+)

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

Running the AI golden-set eval (needs a real `ANTHROPIC_API_KEY` in `.env`,
costs a small amount of real API usage, not run as part of `pytest`):

```bash
cd backend
python -m eval.run_eval
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
