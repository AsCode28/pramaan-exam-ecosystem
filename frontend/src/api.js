// Single source of truth for the backend base URL.
// Override per environment with VITE_API_BASE_URL in a .env file.
const RAW_API_BASE =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export const API_BASE = String(RAW_API_BASE).replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function extractErrorDetail(data, res) {
  const detail = data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d) => d?.msg || JSON.stringify(d)).join("; ");
  }
  return `HTTP ${res.status} ${res.statusText || "request failed"}`.trim();
}

async function request(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: "omit",
      ...(body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
  } catch {
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}${path}. Is the API running?`,
      0,
    );
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) throw new ApiError(extractErrorDetail(data, res), res.status);
  return data;
}

const post = (path, body = {}) => request(path, { method: "POST", body });
const get = (path) => request(path);

/* ---- Demo scenario ---- */

// -> { exam_id, node_id, candidate_ids[], question_ids[] }
export const createDemoScenario = () => post("/demo/scenario/create");

// -> read-only operational snapshot (exam, nodes, sessions, incidents, audit)
export const getDemoOverview = (examId) => get(`/demo/overview/${examId}`);

/* ---- Demo controls ---- */

export const failNode = (nodeId, reason) =>
  post(`/demo/nodes/${nodeId}/fail`, { reason });

export const recoverNode = (nodeId, reason) =>
  post(`/demo/nodes/${nodeId}/recover`, { reason });

// -> { tampered_sequence_no, field, detail }
export const simulateTampering = (field = "payload") =>
  post("/demo/tamper", { target: "latest", field });

/* ---- Sessions ---- */

export const startSession = ({ examId, candidateId, nodeId }) =>
  post("/session/start", {
    exam_id: examId,
    candidate_id: candidateId,
    node_id: nodeId,
  });

export const saveAnswer = (sessionId, { questionId, answer, clientEventId, clientTimestamp }) =>
  post(`/session/${sessionId}/answer`, {
    question_id: questionId,
    answer,
    client_event_id: clientEventId,
    client_timestamp: clientTimestamp,
  });

export const reconcileSession = (sessionId, events) =>
  post(`/session/${sessionId}/reconcile`, { events });

/* ---- Incidents ---- */

// -> { incidents: IncidentResponse[], total }
export const listIncidents = (examId) => get(`/incidents?exam_id=${examId}`);

// -> AIAnalysisResponse (503 when Gemini is not configured)
export const analyzeIncident = (incidentId) =>
  post(`/incident/${incidentId}/analyze`);

/* ---- Audit ---- */

// -> AuditVerifyResponse
export const verifyAudit = () => post("/audit/verify");
