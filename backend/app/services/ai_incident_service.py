"""Grounded AI incident analysis (explanatory only, strictly read-only).

Sends ONLY the structured evidence package built by ``evidence_service`` to
Gemini via the official ``google-genai`` Python SDK, requires strict JSON output
matching a Pydantic schema, then independently re-validates the response and
grounds every ``evidence_refs`` entry against the incident's real evidence
sequence numbers.

Guarantees
----------
- Explanatory only: never creates or updates Event, Incident, Session, Node,
  Response or audit state. The database session is only read from.
- Never returns an ungrounded evidence reference: a single hallucinated
  reference rejects the whole response.
- Every AI failure mode (missing key, SDK/API error, timeout, invalid JSON,
  missing fields, ungrounded refs) raises a controlled exception so the API can
  surface a controlled error without affecting core incident/evidence/audit
  functionality.
- Credentials are read from the ``GEMINI_API_KEY`` environment variable and are
  never hard-coded.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session as DbSession

from app.db.incident import Incident
from app.services import evidence_service

# Default Gemini model; override with the GEMINI_MODEL environment variable.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_MODEL_ENV = "GEMINI_MODEL"

REQUIRED_RESPONSE_FIELDS = (
    "likely_cause",
    "impact_summary",
    "recommended_response",
    "evidence_refs",
)


class GeminiNotConfiguredError(RuntimeError):
    """GEMINI_API_KEY is absent or empty in the environment."""


class GeminiRequestError(RuntimeError):
    """The Gemini SDK/API call failed (network, auth, timeout, quota, ...)."""


class AIResponseInvalidError(RuntimeError):
    """Model output was unusable, incomplete, or not grounded in the evidence."""


class IncidentAnalysis(BaseModel):
    """Strict schema requested from Gemini and re-validated server-side."""

    likely_cause: str
    impact_summary: str
    recommended_response: str
    evidence_refs: list[int] = Field(default_factory=list)


def get_model_name() -> str:
    """Resolve the Gemini model name from the environment."""
    return os.getenv(GEMINI_MODEL_ENV) or DEFAULT_GEMINI_MODEL


def get_api_key() -> str:
    """Read GEMINI_API_KEY from the environment. Never hard-coded."""
    api_key = os.getenv(GEMINI_API_KEY_ENV)
    if api_key is None or not api_key.strip():
        raise GeminiNotConfiguredError(
            f"{GEMINI_API_KEY_ENV} is not set; AI incident analysis is unavailable"
        )
    return api_key.strip()


def _build_prompt(evidence_package: dict[str, Any]) -> str:
    """Render ONLY the structured evidence package as the model context."""
    return (
        "You are an exam-infrastructure analyst. You receive a single JSON "
        "evidence package describing one incident from an append-only, "
        "hash-chained event ledger.\n\n"
        "Rules:\n"
        "- Explain the incident using ONLY the supplied evidence. Never invent "
        "facts that are absent from the package.\n"
        "- evidence_refs may only contain sequence_no values that appear in "
        "evidence_events. Never reference anything else.\n"
        "- If the ledger audit status is invalid, say so in impact_summary and "
        "recommend re-verification; do not silently ignore it.\n"
        "- Recovery facts already state what the ledger can prove; do not "
        "infer buffered-answer reconciliation from generic ANSWER_SAVED "
        "events.\n\n"
        "EVIDENCE PACKAGE (JSON):\n"
        f"{json.dumps(evidence_package, indent=2, sort_keys=True, default=str)}\n\n"
        "Return structured JSON matching the required schema."
    )


def _extract_text(response: Any) -> str:
    """Pull raw JSON text out of a Gemini structured-output response."""
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        # Structured output: the SDK already coerced the payload.
        return json.dumps(
            {
                "likely_cause": parsed.likely_cause,
                "impact_summary": parsed.impact_summary,
                "recommended_response": parsed.recommended_response,
                "evidence_refs": list(parsed.evidence_refs),
            }
        )

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        text = "".join(t for t in (getattr(p, "text", None) for p in parts) if t)
        if text.strip():
            return text

    raise AIResponseInvalidError("Gemini returned an empty response")


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ```json ... ``` fence if the model emitted one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    parts = stripped.split("```", 2)
    if len(parts) < 2:
        return stripped
    body = parts[1]
    if body.lstrip().lower().startswith("json"):
        body = body.lstrip()[4:]
    return body.strip()


def _call_gemini(prompt: str) -> Any:
    """Invoke the official google-genai SDK. Isolated so tests can mock it."""
    from google import genai

    client = genai.Client(api_key=get_api_key())
    try:
        return client.models.generate_content(
            model=get_model_name(),
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": IncidentAnalysis,
            },
        )
    except Exception as exc:  # SDK, network, auth, timeout, quota, ...
        raise GeminiRequestError(f"Gemini request failed: {exc}") from exc


def _parse_and_validate(text: str) -> IncidentAnalysis:
    """Parse the model JSON and re-validate required fields server-side."""
    try:
        raw = json.loads(_strip_code_fence(text))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AIResponseInvalidError(
            f"Gemini response was not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise AIResponseInvalidError("Gemini response JSON was not an object")

    missing = [f for f in REQUIRED_RESPONSE_FIELDS if f not in raw]
    if missing:
        raise AIResponseInvalidError(
            f"Gemini response missing required fields: {sorted(missing)}"
        )

    try:
        return IncidentAnalysis.model_validate(raw)
    except ValidationError as exc:
        raise AIResponseInvalidError(
            f"Gemini response failed schema validation: {exc}"
        ) from exc


def _ground_evidence_refs(
    analysis: IncidentAnalysis, allowed_sequence_nos: list[int]
) -> list[int]:
    """Reject the whole response if ANY evidence reference is ungrounded."""
    allowed = set(allowed_sequence_nos)
    ungrounded = [ref for ref in analysis.evidence_refs if ref not in allowed]
    if ungrounded:
        raise AIResponseInvalidError(
            "Gemini returned ungrounded evidence references: "
            f"{sorted(set(ungrounded))}"
        )
    # Deterministic: de-duplicated and ascending.
    return sorted(set(analysis.evidence_refs))


def analyze_incident(db: DbSession, incident: Incident) -> dict[str, Any]:
    """Explain an incident with Gemini. Strictly read-only.

    Raises GeminiNotConfiguredError, GeminiRequestError or AIResponseInvalidError
    on any AI failure. Never mutates Event, Incident, Session, Node, Response or
    audit state.
    """
    get_api_key()  # fail fast and explicitly when unconfigured

    evidence_package = evidence_service.build_evidence_package(db, incident)
    allowed_sequence_nos = [
        event["sequence_no"] for event in evidence_package["evidence_events"]
    ]

    prompt = _build_prompt(evidence_package)
    try:
        response = _call_gemini(prompt)
    except (
        GeminiNotConfiguredError,
        GeminiRequestError,
        AIResponseInvalidError,
    ):
        raise
    except Exception as exc:  # defensive: any unexpected SDK/client failure
        raise GeminiRequestError(f"Gemini request failed: {exc}") from exc

    analysis = _parse_and_validate(_extract_text(response))
    evidence_refs = _ground_evidence_refs(analysis, allowed_sequence_nos)

    return {
        "incident_id": incident.id,
        "likely_cause": analysis.likely_cause,
        "impact_summary": analysis.impact_summary,
        "recommended_response": analysis.recommended_response,
        "evidence_refs": evidence_refs,
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
