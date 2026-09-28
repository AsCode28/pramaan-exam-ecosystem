# pramaan-exam-ecosystem

## Backend for the PRAMAAN exam resilience prototype: 
An append-only,
hash-chained event ledger with deterministic incident detection, tamper
detection, and a grounded AI explanation layer.

This is a **local screening prototype running on SQLite**. It is not
production-ready and makes no claims about regulatory, encryption or offline
capabilities — see [Not implemented](#not-implemented) below.

## What it actually does

**Append-only, hash-chained event ledger.**
Every meaningful action (session start, answer, heartbeat, node failure,
recovery, reconciliation) appends an immutable row. Each row stores
`H_i = SHA256(canonical_event_i || H_{i-1})`, where the first event anchors to a
deterministic genesis digest. Nothing updates or deletes a committed event.

**Authoritative server timestamps.**
`Event.server_timestamp` is generated backend-side and is the only clock used
for ordering, freshness and incident windows. `client_timestamp` is retained as
untrusted diagnostic metadata and is never used to make a decision.

**Response projection.**
`Response` is a current-state projection of the ledger, advanced only forward
(`last_event_id` monotonic). The event ledger remains the source of truth; a
projection can be rebuilt by replaying events in `sequence_no` order.

**Session recovery and reconciliation.**
A node failure disconnects its `ACTIVE` and `RECOVERING` sessions. Recovering
the node moves them to `RECOVERING`; the client replays its buffered answers,
which are validated, de-duplicated and projected before the session returns to
`ACTIVE` with a `SESSION_RECOVERED` event. A clean reconciliation with an empty
buffer is a valid recovery.

**Deterministic incident detection.**
A read-only projection over the ledger, session states and node states produces
three independent scopes: `CANDIDATE` (per affected session), `NODE` (per
origin node, anchored to the episode that is still failing) and `SYSTEM` (per
exam). Incidents are grounded in real events and reused until they resolve.

**Audit verification and tamper detection.**
`POST /audit/verify` walks the global chain in `sequence_no` order, recomputes
every hash, checks sequence continuity and predecessor links, and reports the
**first** broken event. A single edited payload or hash is detected.

**Evidence package.**
`GET /incident/{id}/evidence` returns the ordered evidence events with full
details, the affected sessions/nodes, deterministic recovery facts, and the
global audit status. Evidence selection is single-sourced from the incident
engine.

**Grounded Gemini analysis.**
`POST /incident/{id}/analyze` sends only the evidence package to Gemini
(`google-genai` 2.25.0) with a strict JSON schema. The response is re-validated
server-side: every `evidence_ref` must exist in that incident's evidence
package, and an empty reference list is rejected. **The model is fail-closed —
it never mutates ledger, session, incident or audit state, and an ungrounded
answer is refused rather than filtered.** Requests use a finite, configurable
timeout (`GEMINI_TIMEOUT_MS`, default 30000 ms) so a hung provider cannot pin
a worker.

**Early-warning heartbeat signal.**
`GET /demo/nodes/{node_id}/health` derives freshness from the last authoritative
`HEARTBEAT` event and reports `HEALTHY`, `DEGRADED` (early warning) or `FAILED`,
with the age, threshold and a human-readable reason. It is read-only: no
scheduler, no synthetic events, and no mutation from a GET.

**Demo-mode-only operations.**
Destructive endpoints — node failure injection, recovery simulation, ledger
tamper and `POST /demo/reset` — require `DEMO_MODE=true` and are otherwise
rejected with `403`. `DEMO_MODE` defaults to **off**. Read-only monitoring stays
available regardless. There is no authentication; keep this backend on a local
or trusted network.

## Quick start

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env          # set GEMINI_API_KEY to enable /analyze
DEMO_MODE=true uvicorn app.main:app --port 8000
```

Deterministic end-to-end walkthrough (public HTTP API only, standard library):

```bash
python scripts/demo_walkthrough.py --skip-ai     # requires an empty ledger
python scripts/demo_walkthrough.py --reset-demo  # DEMO_MODE=true; wipes + reruns
```

Tests:

```bash
python -m pytest tests -q
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///pramaan.db` | Local SQLite database |
| `GEMINI_API_KEY` | unset | Enables `/analyze`; unset ⇒ controlled `503` |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model override |
| `GEMINI_TIMEOUT_MS` | `30000` | Finite request timeout (ms) |
| `HEARTBEAT_STALE_SECONDS` | `45` | Stale-heartbeat threshold |
| `DEMO_MODE` | `false` | Gates destructive demo operations |
| `CORS_ALLOW_ORIGINS` | `localhost:3000,5173` | Allowed browser origins |

## Known limitations

**Single-process concurrency only.** The ledger append, the response projection
and incident evaluation are serialised with in-process `threading.Lock`s. This
is correct for one threaded worker, and explicitly **not** a distributed lock:
multiple processes or replicas would still race. Multi-replica deployment needs
database-level coordination (row locks, serialising transactions, or a unique
constraint on the predecessor hash).

**SQLite only.** No PostgreSQL/Redis, no migration tooling. `POST /demo/reset`
refuses any non-SQLite URL.

**No background workers.** Incident evaluation is triggered after
failure/recovery/reconciliation, not on a timer. Early-warning health is
evaluated on read. Nothing runs on a scheduler.

**No authentication or authorization.** Every endpoint is open. `DEMO_MODE` is
a deployment switch, not a security boundary.

**No exam submission or compensatory time.** The session lifecycle stops at
`ACTIVE`; there is no submit flow, scoring, or time-extension policy.

## Not implemented

These are **not** part of this prototype, contrary to any earlier wording:

- **No encryption at rest or in transit.** Data sits in a plain SQLite file.
  There is no application-level encryption, key management, or TLS
  termination in this repository.
- **No offline-first architecture.** There is no local client-side store, no
  background sync engine, and no conflict resolution. "Buffered answers" are
  client-supplied replay payloads accepted by the reconciliation endpoint, not
  an offline persistence layer.
- **No predictive ML.** The early-warning signal is a deterministic
  heartbeat-freshness threshold, not a model.
- **No legal or regulatory policy engine.**


---

## Interactive PRAMAAN Dashboard (Frontend)

To demonstrate the backend's resilience mechanisms visually, this prototype includes a dedicated React-based frontend featuring a built-in **PRAMAAN Demo Controller**:
* **Candidate View:** A pristine, NTA-style examination interface that securely freezes the exam timer during network disruptions.
* **Admin Command Center:** A disaster control hub to inject node failures, trigger the hash-chain audit, and generate AI-driven incident reports.
* **Split View (Demo):** A side-by-side layout designed specifically for hackathon pitches to showcase real-time interactions between backend disruptions and frontend recovery.

### Frontend Quick Start

```bash
cd frontend
npm install
npm run dev
```
Open http://localhost:5173 in your browser and select the Split View (Demo) mode from the top controller strip to begin the visual walkthrough.
