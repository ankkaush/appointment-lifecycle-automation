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
- [x] Phase 4 — customer web chat (bounded clarification loop + thin chat UI)
- [x] Phase 5 — calendar provider abstraction (mock; Google Calendar deferred)
- [x] Phase 6 — background jobs: reminders, no-show detection, recovery, expiring abandoned conversations
- [x] Phase 7 — cancellation & rescheduling
- [x] Phase 8 — real notification provider
- [x] **Phase 9** — business dashboard
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

Phase 5 detail: `app/workflow/calendar.py` adds the `CalendarProvider`
Protocol (`create_event` / `update_event` / `cancel_event` — three
methods, matching the architecture baseline's Section H spec exactly)
and `MockCalendarProvider`, the only implementation for now — a real
Google Calendar adapter is deliberately deferred pending a separate
decision on credential configuration; nothing Google-specific exists
anywhere in this codebase yet. `Appointment` gains `calendar_event_id`
and `calendar_sync_status` (`PENDING`/`SYNCED`/`FAILED`, tracked
independently of `AppointmentStatus` so a sync failure can never be
confused with, or block, the booking itself). Sync is attempted
synchronously right after a booking commits in `confirm_slot`, but a
`CalendarProviderError` never invalidates the booking and never skips
the customer's confirmation notification, which still fires regardless.
Sync events reuse the existing `AuditEvent` table rather than a new one.
`retry_calendar_sync` (plus `POST /v1/appointments/{id}/retry-calendar-
sync`) is a manual, idempotent retry — not an automated sweep, which
belongs with Phase 6's background-job infrastructure once it exists;
retrying an already-`SYNCED` appointment is a harmless no-op that never
even calls the provider. `app/domain/availability.py` and
`app/domain/booking.py` are untouched — the calendar has no path back
into availability or conflict-prevention logic. No live Anthropic calls
in this phase's implementation.

Phase 6 detail: `app/workflow/jobs.py` splits background work into two
shapes -- `ScheduledJob` (one-off, entity-specific: currently just
`SEND_REMINDER`, claimed with `SELECT ... FOR UPDATE SKIP LOCKED` so more
than one worker process can run safely) and plain periodic sweep
functions for recurring maintenance (no-show detection, expiring
abandoned runs), which don't need a row per run. `app/worker.py` is a
second process running the same image on a 60s poll loop -- no Redis, no
broker, coordinating purely through Postgres, per the standing
instruction against unnecessary infrastructure; `POST
/v1/jobs/run-tick` runs the same logic on demand. `app/domain/noshow.py`
implements the deterministic no-show rule designed back in the
architecture review (`now > end_at + grace_period → NO_SHOW`) as a pure
predicate.

The most novel piece: no-show recovery reuses Phase 4's clarification
machinery almost entirely rather than duplicating it. A `ProcessingRun`
gained a `kind` (`CUSTOMER_INITIATED`/`RECOVERY`) and
`recovery_of_appointment_id`; a recovery run is created by the system
(not a customer message), pre-fills `matched_service_id` from the missed
appointment, and sits in `AWAITING_CLARIFICATION` waiting on a reply
through the exact same `reply_to_clarification` / `POST /reply` path a
clarifying question uses -- no parallel endpoint, no parallel state. One
deliberate widening: a recovery run accepts `RESCHEDULE` intent as well
as `BOOK` (an ordinary run still only accepts `BOOK` -- confirmed by
test), since a customer replying "Thursday instead?" to a recovery
message and a customer saying "book me for Thursday" mean the same thing
here, whichever word the model reaches for. A successful rebooking sets
the new `Appointment.rebooked_from_id`; the original stays immutably
`NO_SHOW`. `app/domain/availability.py`, `app/domain/booking.py`,
`app/workflow/calendar.py`, and `app/workflow/clarify.py` are all
untouched — confirmed via `git diff --stat` showing zero changes to any
of them. No live Anthropic calls in this phase's implementation.

Phase 7 detail: `app/domain/appointments.py` adds `cancel_appointment` and
`reschedule_appointment`, the two mutations that act on an existing,
already-BOOKED `Appointment` in place rather than creating a new one --
rescheduling preserves the same row, same id, same history. Both share one
policy predicate, `can_modify`, gated on a single new setting,
`Business.min_reschedule_notice_hours` (cancellation and rescheduling
share one notice-window knob, not two, since they ask the same "how much
notice do we need" question). Rescheduling reuses `booking.py`'s two-layer
concurrency discipline -- a deterministic availability re-check, backed by
the real guarantee, the same Postgres exclusion constraint -- without
duplicating `booking.py`'s insert path or touching it; the one shared
change `availability.py` needed was an optional `exclude_appointment_id`,
so a reschedule's re-check doesn't collide with the very row it's moving.

Resolving *which* appointment a cancel/reschedule request means is
deliberately simple for the MVP, per the approved design: exactly one
upcoming `BOOKED` appointment is acted on automatically; zero or several
is a clean escalation, not a second clarification conversation -- the
data model still allows a customer to have more than one appointment, this
is only the automated flow declining to guess. `app/workflow/orchestrator.py`
routes a confident (non-ambiguous) `CANCEL`/`RESCHEDULE` intent to this
policy check; an *ambiguous* one still falls through to the same immediate
escalation a confident unacceptable intent gets today -- Phase 7
deliberately doesn't extend the bounded clarification loop to "which
appointment" or "cancel vs. reschedule" uncertainty. A successful
reschedule reuses the exact same slot-offering / `AWAITING_CONFIRMATION`
machinery a fresh booking uses (a new `ProcessingRun.target_appointment_id`
field tells `confirm_slot` to move the existing appointment instead of
booking a new one); a successful cancellation is immediate and
deterministic, with no offer/confirm step needed. Both call
`CalendarProvider.update_event()` / `cancel_event()` (defined since Phase
5, unused until now) with the same best-effort, never-blocking discipline
as Phase 5's `create_event` sync -- a calendar failure is recorded and
retryable, never a reason to undo a cancellation or reschedule that already
committed in Postgres. A reschedule also moves the appointment's pending
`SEND_REMINDER` job to the new time, so Phase 6's reminder doesn't fire
against a start time that no longer exists. `app/domain/availability.py`'s
one addition aside, `app/domain/booking.py` and `app/domain/noshow.py` are
untouched. No live Anthropic calls in this phase's implementation.

Phase 8 detail: `app/workflow/notifications.py` gains `ConsoleNotificationProvider`
(logs and persists, no credentials -- the new local-dev default) alongside
the unchanged `NotificationService` Protocol and `MockNotificationProvider`
tests use. The real vendor, Resend, lives in its own module,
`app/workflow/notifications_resend.py` -- the only file that imports
`httpx` for this, matching how `app/ai/providers/claude.py` is the only
module that imports the Anthropic SDK. All three implementations share the
same five-method Protocol; message content (subject/body per notification
type) is composed once in `notifications.py` and reused by all three,
rather than duplicated per provider. A new composition root,
`app/workflow/notification_dependency.py`, reads `NOTIFICATION_PROVIDER`
(`console` / `resend` / `mock`) and is now what both `routes_workflow.py`
and `app/worker.py` depend on -- previously `app/worker.py` hardcoded
`MockNotificationProvider()` directly, which would have silently defeated
this phase for reminders and no-show recovery outreach (arguably the two
most important notifications) had it been left as-is.

`Customer.contact` has no channel marker (email vs. phone) by design since
Phase 4, and Resend is email-only: rather than adding client-side
validation, a contact that isn't a deliverable address is simply a send
that fails. `ResendNotificationProvider` catches that -- any `httpx`
error or non-2xx response -- internally and records the `Notification` as
`FAILED` instead of raising, the same discipline `CalendarProviderError`
handling uses for calendar sync: a failed send is recorded and never
blocks or unwinds the appointment action that triggered it, and
`NotificationStatus.FAILED` (unused since Phase 3) finally means
something. This keeps every existing call site in `orchestrator.py` and
`jobs.py` completely unchanged -- the failure handling lives entirely
inside the provider, not at each of the five call sites. No manual retry
endpoint for a failed notification yet (unlike calendar's
`retry-calendar-sync`) -- deferred until an actual need shows up, per the
same "keep scope focused" discipline as every other phase.

Not yet exercised against the real Resend API (no live credentials
configured) -- same deliberate deferral as the real Google Calendar
adapter in Phase 5. `ResendNotificationProvider` accepts an optional
`httpx` transport for exactly this reason: tests inject an
`httpx.MockTransport` to verify the success and failure paths without a
real network call, and a real deployment can supply real credentials via
`.env` (`RESEND_API_KEY`, `RESEND_FROM_EMAIL`) without any code changes.
No live Anthropic calls in this phase's implementation.

Phase 9 detail: `frontend/` is a new Next.js (App Router, TypeScript) app --
the first frontend infrastructure in this codebase beyond the framework-
free chat UI. It's a single Client Component page (`app/page.tsx`)
showing one business's escalation queue: reason, customer name/contact,
the original message, and a Resolve action, talking directly to the
existing `GET /v1/escalations` / `POST /v1/escalations/{id}/resolve`
endpoints through a thin typed wrapper (`lib/api.ts`).

No login exists anywhere in this system yet, and building one was
explicitly out of scope for this phase (that's real auth, Phase 10's
"security hardening"). Given that, the dashboard trusts a `business_id`
the same way the rest of the API already does -- entered by hand for now,
persisted in the browser's `localStorage` for convenience across reloads.
That surfaced a real, pre-existing gap rather than one this phase
introduced: `GET /v1/escalations` had no business scoping at all before
now -- any caller could see every business's escalation queue. Both
endpoints now require `business_id` and filter/verify by it; resolving an
escalation for the wrong business returns the same 404 as one that
doesn't exist, rather than confirming which UUIDs are real for someone
else's business. The list endpoint also gained a proper response shape
(`EscalationQueueItemOut`, joined through `ProcessingRun` to `Customer`)
-- the original `EscalationCaseOut` was just an opaque `processing_run_id`
and a reason, not enough for a human to act on.

`app/main.py` gained `CORSMiddleware`, scoped to the dashboard's origin
only -- the first cross-origin browser client this API has ever had (the
chat UI is same-origin, mounted directly into the FastAPI app). A new
`dashboard` service in `docker-compose.yml` runs the Next.js dev server
on port 3010, alongside `db`/`api`/`worker`, wired the same way: build
context, volume-mounted source, no new infrastructure component beyond
the one new container. No live Anthropic calls in this phase's
implementation.

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
it directly (e.g. with `psql`). The business dashboard comes up at
`http://localhost:3010` — enter a `business_id` (from `POST /v1/businesses`)
to see that business's escalation queue.

Running the frontend directly (outside Docker):

```bash
cd frontend
cp .env.local.example .env.local
npm install
npm run dev -- -p 3010
```

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
