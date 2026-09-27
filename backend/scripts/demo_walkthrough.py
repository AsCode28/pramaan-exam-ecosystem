"""Deterministic end-to-end demo walkthrough for the PRAMAAN exam ecosystem.

Drives the real public HTTP API of a running backend and validates the full
resilience story: scenario -> sessions -> answers -> node failure -> incident
detection -> recovery -> reconciliation -> incident resolution -> audit
verification -> evidence -> (optional) grounded AI analysis -> overview.

This runner is intentionally black-box: it uses ONLY the public HTTP API.
It never imports the application, never opens the database and never calls a
service layer, so passing proves the shipped API contract end to end.

It uses only the Python standard library (urllib + json) so no runtime
dependency is added just for the demo.

Usage (with a backend already running, e.g. `uvicorn app.main:app`):

    python scripts/demo_walkthrough.py
    python scripts/demo_walkthrough.py --base-url http://127.0.0.1:8000
    python scripts/demo_walkthrough.py --skip-ai        # no Gemini configured

Exit codes: 0 on success, 1 on any failed check or HTTP error.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
HTTP_TIMEOUT_SECONDS = 30


class DemoFailure(Exception):
    """Any validation failure, transport error or unexpected state."""


class DemoClient:
    """Minimal JSON client over the standard library."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8").strip()
            except Exception:  # pragma: no cover - defensive
                pass
            raise DemoFailure(
                f"{method} {path} returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise DemoFailure(
                f"cannot reach the backend at {self.base_url} ({exc.reason}). "
                "Start it first, e.g. `uvicorn app.main:app --port 8000`, "
                "or pass --base-url."
            ) from exc
        except TimeoutError as exc:
            raise DemoFailure(f"{method} {path} timed out") from exc

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DemoFailure(
                f"{method} {path} returned non-JSON body: {raw[:200]!r}"
            ) from exc

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body if body is not None else {})


def require_fields(payload: dict, fields: list[str], context: str) -> None:
    """Fail if any expected field is missing from an API response."""
    missing = [f for f in fields if f not in payload]
    if missing:
        raise DemoFailure(f"{context}: response is missing field(s) {missing}")


def require(condition: bool, message: str) -> None:
    """Fail with a clear message when a check does not hold."""
    if not condition:
        raise DemoFailure(message)



def stage(number: int, title: str) -> None:
    """Print a clear stage banner."""
    print(f"\n[{number}/11] {title}")
    print("-" * (len(title) + 8))


def ok(message: str) -> None:
    print(f"    OK  {message}")


def stage_create_scenario(client: DemoClient) -> dict:
    """POST /demo/scenario/create -> one exam, node, 3 candidates, 3 questions."""
    scenario = client.post("/demo/scenario/create")
    require_fields(
        scenario,
        ["exam_id", "node_id", "candidate_ids", "question_ids"],
        "scenario/create",
    )
    require(
        len(scenario["candidate_ids"]) == 3,
        f"scenario/create: expected 3 candidates, got {scenario['candidate_ids']}",
    )
    require(
        len(scenario["question_ids"]) == 3,
        f"scenario/create: expected 3 questions, got {scenario['question_ids']}",
    )
    ok(
        f"exam_id={scenario['exam_id']} node_id={scenario['node_id']} "
        f"candidates={scenario['candidate_ids']} questions={scenario['question_ids']}"
    )
    return scenario


def stage_start_sessions(client: DemoClient, scenario: dict) -> list[dict]:
    """POST /session/start for all 3 candidates -> three ACTIVE sessions."""
    sessions = []
    for candidate_id in scenario["candidate_ids"]:
        body = client.post(
            "/session/start",
            {
                "exam_id": scenario["exam_id"],
                "candidate_id": candidate_id,
                "node_id": scenario["node_id"],
            },
        )
        require_fields(body, ["session_id", "status"], "session/start")
        require(
            body["status"] == "ACTIVE",
            f"session/start for candidate {candidate_id}: "
            f"expected ACTIVE, got {body['status']!r}",
        )
        sessions.append(body)
    require(len(sessions) == 3, f"expected 3 sessions, got {len(sessions)}")
    ok(f"3 sessions ACTIVE: {[s['session_id'] for s in sessions]}")
    return sessions


def stage_save_answers(client: DemoClient, scenario: dict, sessions: list[dict]) -> None:
    """POST /session/{id}/answer -> real answers stored on the ledger."""
    for index, session in enumerate(sessions):
        body = client.post(
            f"/session/{session['session_id']}/answer",
            {
                "question_id": scenario["question_ids"][0],
                "answer": "A",
                "client_event_id": f"walkthrough-live-{index}",
            },
        )
        require_fields(body, ["sequence_no", "current_answer"], "session/answer")
        require(
            body["current_answer"] == "A",
            f"session/answer for session {session['session_id']}: "
            f"expected current_answer 'A', got {body['current_answer']!r}",
        )
    ok("3 live answers stored on the ledger")


def stage_fail_node_and_detect(
    client: DemoClient, scenario: dict, sessions: list[dict]
) -> list[dict]:
    """Fail the node, evaluate incidents, verify DISCONNECTED + 3 severities."""
    failed = client.post(
        f"/demo/nodes/{scenario['node_id']}/fail", {"reason": "demo walkthrough"}
    )
    require_fields(failed, ["new_status", "affected_session_ids"], "nodes/fail")
    require(
        failed["new_status"] == "FAILED",
        f"nodes/fail: expected FAILED, got {failed['new_status']!r}",
    )
    expected = sorted(s["session_id"] for s in sessions)
    require(
        sorted(failed["affected_session_ids"]) == expected,
        f"nodes/fail: expected affected sessions {expected}, "
        f"got {sorted(failed['affected_session_ids'])}",
    )
    ok(f"node FAILED, disconnected sessions {expected}")

    evaluated = client.post(
        "/demo/incidents/evaluate", {"exam_id": scenario["exam_id"]}
    )
    require_fields(evaluated, ["incidents"], "incidents/evaluate")
    incidents = evaluated["incidents"]
    severities = {i["severity"] for i in incidents}
    expected_severities = {"CANDIDATE", "NODE", "SYSTEM"}
    require(
        severities == expected_severities,
        f"incidents/evaluate: expected severities {sorted(expected_severities)}, "
        f"got {sorted(severities)}",
    )
    for incident in incidents:
        require(
            incident["status"] == "ACTIVE",
            f"incident {incident['id']} ({incident['severity']}): "
            f"expected ACTIVE, got {incident['status']!r}",
        )
        require(
            incident["evidence_event_sequence_nos"],
            f"incident {incident['id']} ({incident['severity']}) has no evidence",
        )
    ok(
        f"{len(incidents)} incidents ACTIVE with severities {sorted(severities)}, "
        "all grounded in evidence"
    )
    return incidents


def stage_recover_node(client: DemoClient, scenario: dict) -> None:
    """Recover the node -> HEALTHY and every affected session RECOVERING."""
    recovered = client.post(
        f"/demo/nodes/{scenario['node_id']}/recover", {"reason": "demo walkthrough"}
    )
    require_fields(recovered, ["new_status"], "nodes/recover")
    require(
        recovered["new_status"] == "HEALTHY",
        f"nodes/recover: expected HEALTHY, got {recovered['new_status']!r}",
    )
    ok(f"node HEALTHY, sessions RECOVERING: {recovered['affected_session_ids']}")


def stage_reconcile(client: DemoClient, scenario: dict, sessions: list[dict]) -> None:
    """POST /session/{id}/reconcile -> every session back to ACTIVE."""
    for index, session in enumerate(sessions):
        buffered = [
            {
                "client_event_id": f"walkthrough-buffered-{index}-{k}",
                "question_id": question_id,
                "answer": "B",
            }
            for k, question_id in enumerate(scenario["question_ids"][:2])
        ]
        report = client.post(
            f"/session/{session['session_id']}/reconcile", {"events": buffered}
        )
        require_fields(
            report,
            ["reconciliation_complete", "status", "recovered_event_sequence_no"],
            "session/reconcile",
        )
        require(
            report["reconciliation_complete"] is True,
            f"session/reconcile for session {session['session_id']}: "
            f"reconciliation_complete was {report['reconciliation_complete']!r} "
            f"(missing={report.get('missing_client_event_ids')}, "
            f"mismatched={report.get('mismatched_client_event_ids')}, "
            f"rejected={report.get('rejected_client_event_ids')})",
        )
        require(
            report["status"] == "ACTIVE",
            f"session/reconcile for session {session['session_id']}: "
            f"expected status ACTIVE, got {report['status']!r}",
        )
        require(
            report["recovered_event_sequence_no"] is not None,
            f"session/reconcile for session {session['session_id']}: "
            "no SESSION_RECOVERED event was recorded",
        )
    ok(f"all {len(sessions)} sessions reconciled and ACTIVE")


def stage_resolve_incidents(client: DemoClient, scenario: dict) -> list[dict]:
    """Evaluate again -> every incident RESOLVED."""
    evaluated = client.post(
        "/demo/incidents/evaluate", {"exam_id": scenario["exam_id"]}
    )
    incidents = evaluated["incidents"]
    require(incidents, "incidents/evaluate: expected incidents, got none")
    for incident in incidents:
        require(
            incident["status"] == "RESOLVED",
            f"incident {incident['id']} ({incident['severity']}): "
            f"expected RESOLVED, got {incident['status']!r}",
        )
    ok(f"all {len(incidents)} incidents RESOLVED")
    return incidents


def stage_verify_audit(client: DemoClient) -> dict:
    """POST /audit/verify -> valid chain, nothing broken."""
    audit = client.post("/audit/verify")
    require_fields(
        audit,
        ["valid", "events_checked", "first_broken_sequence_no", "failure_reason"],
        "audit/verify",
    )
    require(audit["valid"] is True, f"audit/verify: chain is invalid: {audit}")
    require(
        audit["first_broken_sequence_no"] is None,
        f"audit/verify: broken at {audit['first_broken_sequence_no']}: "
        f"{audit['failure_reason']}",
    )
    require(
        audit["events_checked"] > 0,
        f"audit/verify: expected a non-empty ledger, got {audit}",
    )
    ok(f"global ledger valid across {audit['events_checked']} events")
    return audit


def stage_fetch_evidence(client: DemoClient, incident_id: int) -> dict:
    """GET /incident/{id}/evidence -> grounded evidence + real recovery facts."""
    evidence = client.get(f"/incident/{incident_id}/evidence")
    require_fields(
        evidence,
        ["incident", "evidence_events", "recovery_facts", "audit_status"],
        "incident/evidence",
    )
    require(
        evidence["evidence_events"],
        f"incident {incident_id}: evidence package is empty",
    )
    require(
        evidence["audit_status"]["valid"] is True,
        f"incident {incident_id}: embedded audit status is invalid: "
        f"{evidence['audit_status']}",
    )
    facts = evidence["recovery_facts"]
    require(
        facts.get("node_failure_count", 0) >= 1,
        f"incident {incident_id}: recovery facts record no node failure",
    )
    require(
        facts.get("node_recovery_count", 0) >= 1,
        f"incident {incident_id}: recovery facts record no node recovery",
    )
    require(
        facts.get("session_recovered_event_count", 0) >= 1,
        f"incident {incident_id}: recovery facts record no SESSION_RECOVERED event",
    )
    ok(
        f"incident {incident_id}: {len(evidence['evidence_events'])} evidence events, "
        f"{facts['node_failure_count']} failure / {facts['node_recovery_count']} recovery "
        f"/ {facts['session_recovered_event_count']} session-recovered"
    )
    return evidence


def stage_analyze(client: DemoClient, incident_id: int, evidence: dict) -> None:
    """POST /incident/{id}/analyze -> grounded analysis (unless --skip-ai)."""
    analysis = client.post(f"/incident/{incident_id}/analyze")
    require_fields(
        analysis,
        [
            "incident_id",
            "likely_cause",
            "impact_summary",
            "recommended_response",
            "evidence_refs",
        ],
        "incident/analyze",
    )
    require(
        analysis["incident_id"] == incident_id,
        f"incident/analyze: expected incident_id {incident_id}, "
        f"got {analysis['incident_id']}",
    )
    for field in ("likely_cause", "impact_summary", "recommended_response"):
        require(
            isinstance(analysis[field], str) and analysis[field].strip(),
            f"incident/analyze: {field} is empty",
        )
    allowed = {e["sequence_no"] for e in evidence["evidence_events"]}
    ungrounded = [ref for ref in analysis["evidence_refs"] if ref not in allowed]
    require(
        not ungrounded,
        f"incident/analyze: ungrounded evidence_refs {ungrounded} are not present "
        f"in the evidence package (allowed: {sorted(allowed)})",
    )
    ok(
        f"incident {incident_id}: analysis grounded, "
        f"evidence_refs={analysis['evidence_refs']}"
    )


def stage_overview(client: DemoClient, scenario: dict, sessions: list[dict]) -> None:
    """GET /demo/overview/{exam_id} -> the frontend's final operational view."""
    overview = client.get(f"/demo/overview/{scenario['exam_id']}")
    require_fields(
        overview,
        ["exam", "nodes", "sessions", "incidents", "audit_status"],
        "demo/overview",
    )
    require(
        len(overview["nodes"]) == 1 and overview["nodes"][0]["status"] == "HEALTHY",
        f"demo/overview: expected one HEALTHY node, got {overview['nodes']}",
    )
    statuses = {s["status"] for s in overview["sessions"]}
    require(
        statuses == {"ACTIVE"},
        f"demo/overview: expected all sessions ACTIVE, got {sorted(statuses)}",
    )
    require(
        len(overview["sessions"]) == len(sessions),
        f"demo/overview: expected {len(sessions)} sessions, "
        f"got {len(overview['sessions'])}",
    )
    incident_statuses = {i["status"] for i in overview["incidents"]}
    require(
        incident_statuses == {"RESOLVED"},
        f"demo/overview: expected all incidents RESOLVED, "
        f"got {sorted(incident_statuses)}",
    )
    require(
        overview["audit_status"]["valid"] is True,
        f"demo/overview: audit status is invalid: {overview['audit_status']}",
    )
    ok(
        f"exam {scenario['exam_id']}: node HEALTHY, "
        f"{len(overview['sessions'])} sessions ACTIVE, "
        f"{len(overview['incidents'])} incidents RESOLVED, audit VALID"
    )



def run(base_url: str, skip_ai: bool) -> None:
    """Execute the full walkthrough, validating every stage."""
    client = DemoClient(base_url)

    stage(1, "Create demo scenario")
    scenario = stage_create_scenario(client)

    stage(2, "Start three sessions")
    sessions = stage_start_sessions(client, scenario)

    stage(3, "Save live answers")
    stage_save_answers(client, scenario, sessions)

    stage(4, "Fail the exam node and detect incidents")
    stage_fail_node_and_detect(client, scenario, sessions)

    stage(5, "Recover the exam node")
    stage_recover_node(client, scenario)

    stage(6, "Reconcile buffered answers")
    stage_reconcile(client, scenario, sessions)

    stage(7, "Re-evaluate and resolve incidents")
    resolved = stage_resolve_incidents(client, scenario)

    stage(8, "Verify the global audit chain")
    stage_verify_audit(client)

    stage(9, "Fetch grounded incident evidence")
    incident_id = resolved[0]["id"]
    evidence = stage_fetch_evidence(client, incident_id)

    stage(10, "Grounded AI incident analysis")
    if skip_ai:
        print("    SKIP  --skip-ai requested; no Gemini call attempted")
    else:
        stage_analyze(client, incident_id, evidence)

    stage(11, "Operational overview")
    stage_overview(client, scenario, sessions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic PRAMAAN demo walkthrough against a running "
            "backend, using only the public HTTP API."
        )
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Backend base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--skip-ai",
        action="store_true",
        help=(
            "Skip the Gemini analysis stage, for environments without a "
            "GEMINI_API_KEY configured."
        ),
    )
    args = parser.parse_args(argv)

    print("PRAMAAN DEMO WALKTHROUGH")
    print(f"    base-url : {args.base_url}")
    print(f"    ai stage : {'skipped' if args.skip_ai else 'enabled'}")

    try:
        run(args.base_url, args.skip_ai)
    except DemoFailure as exc:
        print(f"\nPRAMAAN DEMO FAILED: {exc}", file=sys.stderr)
        return 1

    print("\nPRAMAAN DEMO PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

