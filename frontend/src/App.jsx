import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import {
  API_BASE,
  analyzeIncident,
  createDemoScenario,
  failNode,
  getDemoOverview,
  listIncidents,
  reconcileSession,
  recoverNode,
  saveAnswer,
  simulateTampering,
  startSession,
  verifyAudit,
} from "./api";

// The command center polls the read-only overview endpoint continuously.
const OVERVIEW_POLL_MS = 1500;

// Display copy only. The question ids and the question count come from
// POST /demo/scenario/create, never from this template.
const questionCopy = (position) => ({
  textEn: `(Question ${position}) What is the primary fallback mechanism during a network failure in this ecosystem?`,
  textHi: `(प्रश्न ${position}) इस इकोसिस्टम में नेटवर्क विफलता के दौरान प्राथमिक फ़ॉलबैक मैकेनिज्म क्या है?`,
  optionsEn: [
    "Option 1: Auto-submit",
    "Option 2: Local Caching & Sync",
    "Option 3: End Exam",
    "Option 4: Refresh Page",
  ],
  optionsHi: [
    "विकल्प 1: ऑटो-सबमिट",
    "विकल्प 2: लोकल कैशिंग और सिंक",
    "विकल्प 3: परीक्षा समाप्त करें",
    "विकल्प 4: पेज रिफ्रेश करें",
  ],
});

// Buffer entries must be stable client_event_ids so a later reconciliation can
// be matched against what the ledger already acknowledged.
const newClientEventId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `client-event-${Date.now()}-${Math.random().toString(16).slice(2)}`;

const t = {
  en: {
    candName: "Candidate Name",
    examName: "Exam Name",
    subName: "Subject Name",
    remTime: "Remaining Time",
    question: "Question",
    saveNext: "SAVE & NEXT",
    clear: "CLEAR",
    saveMark: "SAVE & MARK FOR REVIEW",
    markNext: "MARK FOR REVIEW & NEXT",
    back: "<< BACK",
    next: "NEXT >>",
    submit: "SUBMIT",
    palette: "Question Palette",
    notVis: "Not Visited",
    notAns: "Not Answered",
    ans: "Answered",
    marked: "Marked",
    ansMarked: "Answered & Marked for Review",
    test: "Test",
  },
  hi: {
    candName: "उम्मीदवार का नाम",
    examName: "परीक्षा का नाम",
    subName: "विषय का नाम",
    remTime: "शेष समय",
    question: "प्रश्न",
    saveNext: "सेव और नेक्स्ट",
    clear: "क्लियर",
    saveMark: "सेव और मार्क फॉर रिव्यू",
    markNext: "मार्क फॉर रिव्यू और नेक्स्ट",
    back: "<< पीछे",
    next: "आगे >>",
    submit: "सबमिट करें",
    palette: "प्रश्न पैलेट",
    notVis: "नहीं देखा गया",
    notAns: "उत्तर नहीं दिया",
    ans: "उत्तर दिया",
    marked: "मार्क किया गया",
    ansMarked: "उत्तर दिया और मूल्यांकन के लिए मार्क किया",
    test: "टेस्ट",
  },
};

export default function App() {
  // Navigation & View Modes: 'candidate' | 'split' | 'admin'
  const [viewMode, setViewMode] = useState("split");
  const [currentScreen, setCurrentScreen] = useState("exam"); // Defaults to exam for direct interaction demo
  const [language, setLanguage] = useState("en");

  // --- Live backend wiring ---
  // bootstrap: 'loading' | 'ready' | 'error'
  const [bootstrap, setBootstrap] = useState({ status: "loading", error: null });
  const [scenario, setScenario] = useState(null); // { examId, nodeId, candidateIds, questionIds }
  const [sessions, setSessions] = useState([]); // real Session rows started via POST /session/start
  const [overview, setOverview] = useState(null); // GET /demo/overview/{exam_id}
  const [overviewError, setOverviewError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [busyAction, setBusyAction] = useState(null);

  // Candidate Exam States
  const [isOffline, setIsOffline] = useState(false);
  const [timeLeft, setTimeLeft] = useState(10800);
  const [currentQIndex, setCurrentQIndex] = useState(0);
  const [selectedOptions, setSelectedOptions] = useState({});
  const [qStatus, setQStatus] = useState([]);
  // Answers captured while the node is unreachable, replayed on reconciliation.
  const [localBuffer, setLocalBuffer] = useState([]);
  const [savedToLedger, setSavedToLedger] = useState(0);

  // Admin & Audit States (all values come from the backend)
  const [incidents, setIncidents] = useState([]);
  const [reconciliationLog, setReconciliationLog] = useState(null);
  const [auditResult, setAuditResult] = useState(null); // AuditVerifyResponse
  const [tamperResult, setTamperResult] = useState(null); // DemoTamperResponse
  const [aiReport, setAiReport] = useState(null); // AIAnalysisResponse
  const [aiError, setAiError] = useState(null);

  const TOTAL_QUESTIONS = scenario?.questionIds?.length ?? 0;
  const questions = useMemo(
    () =>
      (scenario?.questionIds ?? []).map((id, i) => ({ id, ...questionCopy(i + 1) })),
    [scenario],
  );

  // The candidate portal drives the first real session returned by /session/start.
  const activeSession = sessions[0] ?? null;
  const activeSessionId = activeSession?.session_id ?? null;
  const localBufferCount = localBuffer.length;

  // The node drives the disruption banner and the timer freeze, straight from
  // the polled overview rather than from any optimistic local guess.
  const nodeStatus = overview?.nodes?.[0]?.status ?? null;
  const nodeHealth = overview?.nodes?.[0]?.health ?? null;
  const isNodeFailed = nodeStatus === "FAILED";
  const auditStatus = auditResult
    ? auditResult.valid
      ? "VALID"
      : "INVALID"
    : null;

  // --- Bootstrap: one demo scenario + one real session per candidate ---
  // bootstrappedRef guarantees only ONE request sequence is ever created, so a
  // StrictMode remount never provisions a second scenario. mountedRef is
  // re-armed at the top of EVERY effect invocation, so the already-running
  // sequence can still apply its state once the remount happens.
  const bootstrappedRef = useRef(false);
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;

    if (bootstrappedRef.current) {
      // A remount is in progress: do not bootstrap again, but keep
      // mountedRef true so the in-flight sequence can finish.
      return () => {
        mountedRef.current = false;
      };
    }
    bootstrappedRef.current = true;

    (async () => {
      try {
        const created = await createDemoScenario();
        const nextScenario = {
          examId: created.exam_id,
          nodeId: created.node_id,
          candidateIds: created.candidate_ids,
          questionIds: created.question_ids,
        };
        if (!mountedRef.current) return;
        setScenario(nextScenario);
        // Palette is sized from the questions the backend actually provisioned.
        setQStatus(
          Array.from({ length: nextScenario.questionIds.length }, (_, i) =>
            i === 0 ? 1 : 0,
          ),
        );

        // Start one real session per candidate and keep the returned ids.
        const started = [];
        for (const candidateId of nextScenario.candidateIds) {
          const s = await startSession({
            examId: nextScenario.examId,
            candidateId,
            nodeId: nextScenario.nodeId,
          });
          started.push(s);
        }
        if (!mountedRef.current) return;
        setSessions(started);
        setBootstrap({ status: "ready", error: null });
      } catch (err) {
        if (!mountedRef.current) return;
        setBootstrap({ status: "error", error: err.message });
      }
    })();

    return () => {
      mountedRef.current = false;
    };
  }, []);

  // --- Poll GET /demo/overview/{exam_id} as the single source of truth ---
  const refreshOverview = useCallback(async () => {
    if (!scenario?.examId) return;
    try {
      const [overviewData, incidentData] = await Promise.all([
        getDemoOverview(scenario.examId),
        listIncidents(scenario.examId),
      ]);
      setOverview(overviewData);
      setIncidents(incidentData?.incidents ?? []);
      setOverviewError(null);
    } catch (err) {
      setOverviewError(err.message);
    }
  }, [scenario]);

  useEffect(() => {
    if (bootstrap.status !== "ready" || !scenario?.examId) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await refreshOverview();
    };
    tick();
    const id = setInterval(tick, OVERVIEW_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [bootstrap.status, scenario?.examId, refreshOverview]);

  // Timer Freeze Logic (USP)
  useEffect(() => {
    let timer = null;
    const isPaused = isOffline || isNodeFailed;
    if (currentScreen === "exam" && !isPaused && timeLeft > 0) {
      timer = setInterval(() => setTimeLeft((prev) => prev - 1), 1000);
    }
    return () => clearInterval(timer);
  }, [currentScreen, isOffline, isNodeFailed, timeLeft]);

  // Browser Network Detection
  useEffect(() => {
    const handleOnline = () => setIsOffline(false);
    const handleOffline = () => setIsOffline(true);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  const formatTime = (seconds) => {
    const h = Math.floor(seconds / 3600)
      .toString()
      .padStart(2, "0");
    const m = Math.floor((seconds % 3600) / 60)
      .toString()
      .padStart(2, "0");
    const s = (seconds % 60).toString().padStart(2, "0");
    return `${h}:${m}:${s}`;
  };

  // --- Candidate Handlers ---
  // Answers are written to the ledger through the real session id. When the
  // node is unreachable the entry is held in the local buffer instead, so it
  // can be replayed through the real reconciliation endpoint later.
  const persistAnswer = async (questionId, answer) => {
    if (!activeSessionId || questionId == null) return;
    const entry = {
      client_event_id: newClientEventId(),
      question_id: questionId,
      answer,
      client_timestamp: new Date().toISOString(),
    };

    if (isNodeFailed || isOffline) {
      setLocalBuffer((prev) => [...prev, entry]);
      return;
    }

    try {
      // api.saveAnswer() destructures camelCase; the buffer entry stays
      // snake_case because that is the wire format reconciliation posts.
      await saveAnswer(activeSessionId, {
        questionId: entry.question_id,
        answer: entry.answer,
        clientEventId: entry.client_event_id,
        clientTimestamp: entry.client_timestamp,
      });
      setSavedToLedger((n) => n + 1);
    } catch (err) {
      // Keep it buffered rather than dropping a candidate answer on the floor.
      setLocalBuffer((prev) => [...prev, entry]);
      setActionError(`Answer buffered locally: ${err.message}`);
    }
  };

  const handleOptionSelect = (optIndex) => {
    setSelectedOptions((prev) => ({ ...prev, [currentQIndex]: optIndex }));
    persistAnswer(questions[currentQIndex]?.id, String(optIndex));
  };

  const updateStatusAndMove = (newStatus, moveDirection = 1) => {
    const newQStatus = [...qStatus];
    newQStatus[currentQIndex] = newStatus;

    let nextQ = currentQIndex + moveDirection;
    if (nextQ >= 0 && nextQ < TOTAL_QUESTIONS) {
      if (newQStatus[nextQ] === 0) newQStatus[nextQ] = 1;
      setCurrentQIndex(nextQ);
    }
    setQStatus(newQStatus);

    localStorage.setItem(
      "examState",
      JSON.stringify({ selectedOptions, qStatus: newQStatus }),
    );
  };

  // --- Admin API Triggers (real backend, no local fallbacks) ---
  const handleInjectFailure = async () => {
    if (!scenario?.nodeId) return;
    setBusyAction("fail");
    setActionError(null);
    try {
      // UI state is driven by the next overview poll, so nothing is set here
      // until the backend has actually confirmed the failure.
      await failNode(scenario.nodeId, "demo power cut");
      setReconciliationLog(null);
      await refreshOverview();
    } catch (err) {
      setActionError(`Failed to inject node failure: ${err.message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const handleRecoverNode = async () => {
    if (!scenario?.nodeId) return;
    setBusyAction("recover");
    setActionError(null);
    try {
      await recoverNode(scenario.nodeId, "demo recovery");

      // Reconcile every stored session against the real endpoint. Sessions the
      // backend did not move into RECOVERING answer 409, which is reported
      // rather than papered over.
      const perSession = [];
      for (const s of sessions) {
        // Buffered answers belong to the candidate session that created them;
        // the other sessions reconcile with an empty batch, which the backend
        // treats as a clean recovery.
        const buffered =
          s.session_id === activeSessionId
            ? localBuffer.map((b) => ({
                client_event_id: b.client_event_id,
                question_id: b.question_id,
                answer: b.answer,
                client_timestamp: b.client_timestamp,
              }))
            : [];
        try {
          const report = await reconcileSession(s.session_id, buffered);
          perSession.push({ session_id: s.session_id, ok: true, report });
        } catch (err) {
          perSession.push({
            session_id: s.session_id,
            ok: false,
            error: err.message,
          });
        }
      }

      // Counts are summed straight from the reconciliation reports.
      const ok = perSession.filter((r) => r.ok).map((r) => r.report);
      const sum = (key) =>
        ok.reduce((acc, r) => acc + (r[key]?.length ?? 0), 0);
      const completeCount = ok.filter((r) => r.reconciliation_complete).length;

      setReconciliationLog({
        status:
          completeCount === sessions.length && sessions.length > 0
            ? "VERIFIED"
            : "INCOMPLETE",
        sessionsAttempted: sessions.length,
        sessionsReconciled: completeCount,
        submitted: sum("submitted_client_event_ids"),
        acknowledged: sum("acknowledged_client_event_ids"),
        newly: sum("newly_reconciled_client_event_ids"),
        missing: sum("missing_client_event_ids"),
        mismatched: sum("mismatched_client_event_ids"),
        rejected: sum("rejected_client_event_ids"),
        perSession,
      });

      setLocalBuffer([]);
      await refreshOverview();
    } catch (err) {
      setActionError(`Recovery failed: ${err.message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const handleVerifyAudit = async () => {
    setBusyAction("audit");
    setActionError(null);
    try {
      const result = await verifyAudit();
      setAuditResult(result);
    } catch (err) {
      setAuditResult(null);
      setActionError(`Audit verification failed: ${err.message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const handleSimulateTampering = async () => {
    setBusyAction("tamper");
    setActionError(null);
    try {
      // The backend resolves "latest" to the real highest sequence number, so
      // no event id is ever hardcoded here.
      const result = await simulateTampering("payload");
      setTamperResult(result);
    } catch (err) {
      setTamperResult(null);
      setActionError(`Tamper simulation failed: ${err.message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const handleGenerateAiReport = async () => {
    // Pick a real incident from the list the backend just returned.
    const target =
      incidents.find((i) => i.status === "ACTIVE") ?? incidents[incidents.length - 1];
    if (!target) {
      setAiError(
        "No incident available yet. Inject a node failure from this screen first.",
      );
      setAiReport(null);
      return;
    }
    setBusyAction("ai");
    setAiError(null);
    try {
      const report = await analyzeIncident(target.id);
      setAiReport(report);
    } catch (err) {
      setAiReport(null);
      setAiError(`Analysis unavailable: ${err.message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const currentQ = questions[currentQIndex];

  // --- Bootstrap gate: never render a demo shell over a dead backend ---
  if (bootstrap.status !== "ready") {
    return (
      <div className="flex h-screen bg-gray-100 font-sans items-center justify-center p-6">
        <div className="max-w-lg w-full bg-white rounded-lg shadow-lg border border-gray-200 p-6 text-center">
          <h1 className="text-lg font-black text-gray-800 mb-2">
            PRAMAAN DEMO CONTROLLER
          </h1>
          {bootstrap.status === "loading" ? (
            <>
              <p className="text-sm text-gray-600">
                Creating the demo scenario and starting candidate sessions on{" "}
                <span className="font-mono text-xs">{API_BASE}</span>…
              </p>
              <div className="mt-4 h-1.5 w-full bg-gray-200 rounded overflow-hidden">
                <div className="h-full w-1/2 bg-orange-500 animate-pulse" />
              </div>
            </>
          ) : (
            <>
              <p className="text-sm font-semibold text-red-700 mb-2">
                Cannot start the demo.
              </p>
              <p className="text-xs text-gray-600 font-mono break-words">
                {bootstrap.error}
              </p>
              <p className="text-xs text-gray-500 mt-3">
                Start the backend at <span className="font-mono">{API_BASE}</span>{" "}
                and reload this page.
              </p>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen bg-gray-100 font-sans select-none overflow-hidden">
      {/* Connection / API error strip */}
      {(overviewError || actionError) && (
        <div className="bg-red-600 text-white text-center py-1.5 px-4 text-xs font-semibold z-30">
          {overviewError ? `Overview sync error: ${overviewError}` : actionError}
        </div>
      )}

      {/* DEMO MODE CONTROL STRIP */}
      <div className="bg-[#0f172a] text-white px-4 py-1.5 flex justify-between items-center text-xs border-b border-slate-700">
        <div className="flex items-center gap-3">
          <span className="font-extrabold text-orange-400 tracking-wider">
            PRAMAAN DEMO CONTROLLER
          </span>
          <span className="text-gray-400">| View Mode:</span>
          <div className="flex bg-slate-800 rounded p-0.5 border border-slate-600">
            <button
              onClick={() => setViewMode("candidate")}
              className={`px-2 py-0.5 rounded ${viewMode === "candidate" ? "bg-blue-600 text-white font-bold" : "text-gray-400"}`}
            >
              Candidate View
            </button>
            <button
              onClick={() => setViewMode("split")}
              className={`px-2 py-0.5 rounded ${viewMode === "split" ? "bg-orange-600 text-white font-bold" : "text-gray-400"}`}
            >
              Split View (Demo)
            </button>
            <button
              onClick={() => setViewMode("admin")}
              className={`px-2 py-0.5 rounded ${viewMode === "admin" ? "bg-purple-600 text-white font-bold" : "text-gray-400"}`}
            >
              Admin Center
            </button>
          </div>
        </div>
        <div className="flex items-center gap-4 text-gray-300">
          <span className="font-mono text-[10px] text-slate-500">
            exam #{scenario?.examId} · node #{scenario?.nodeId} · session #
            {activeSessionId ?? "—"} · {sessions.length} sessions
          </span>
          <span>
            Node Status:{" "}
            <strong
              className={
                nodeStatus === "FAILED"
                  ? "text-red-400"
                  : nodeStatus === "DEGRADED"
                    ? "text-amber-400"
                    : "text-green-400"
              }
            >
              {nodeStatus ?? "LOADING…"}
            </strong>
          </span>
          <span>
            Buffered Events:{" "}
            <strong className="text-orange-400">{localBufferCount}</strong>
          </span>
          <span>
            Answers Synced:{" "}
            <strong className="text-green-400">{savedToLedger}</strong>
          </span>
        </div>
      </div>

      {/* MAIN DEMO WORKSPACE */}
      <div className="flex flex-1 overflow-hidden">
        {/* ================= LEFT: CANDIDATE EXAM PORTAL ================= */}
        {(viewMode === "candidate" || viewMode === "split") && (
          <div
            className={`flex flex-col bg-white border-r-2 border-slate-300 overflow-hidden ${viewMode === "split" ? "flex-[1.2]" : "flex-1"}`}
          >
            {/* Disruptive Incident Banner (USP) */}
            {(isNodeFailed || isOffline) && (
              <div className="bg-red-600 text-white text-center py-1.5 font-bold animate-pulse text-xs z-20 shadow-md">
                ⚠️ CONNECTION DISRUPTED: Exam Timer Frozen. Answers buffering
                locally in tamper-evident store.
              </div>
            )}

            {/* Official Exam Header */}
            <div className="bg-white border-b border-orange-500 px-4 py-2 flex justify-between items-center shadow-sm">
              <img
                src="/nta-logo.png"
                alt="MP Online Logo"
                className="h-10 object-contain"
              />
              <div className="flex items-center gap-4 text-xs">
                <div className="text-right">
                  <div className="font-bold text-gray-800">
                    {t[language].candName}:{" "}
                    <span className="text-orange-600">
                      Candidate #{activeSession?.candidate_id ?? "—"}
                    </span>
                  </div>
                  <div className="text-gray-500">
                    {t[language].examName}:{" "}
                    <span className="font-semibold text-gray-700">
                      {overview?.exam?.title ?? "—"}
                    </span>
                  </div>
                </div>
                <div
                  className={`px-2.5 py-1 font-bold rounded font-mono text-sm border ${isNodeFailed || isOffline ? "bg-red-100 text-red-700 border-red-300 animate-pulse" : "bg-blue-50 text-blue-800 border-blue-200"}`}
                >
                  {t[language].remTime}: {formatTime(timeLeft)}
                </div>
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  className="border rounded p-1 text-xs outline-none bg-white"
                >
                  <option value="en">English</option>
                  <option value="hi">हिंदी</option>
                </select>
              </div>
            </div>

            {/* Exam Content + Palette */}
            <div className="flex flex-1 overflow-hidden">
              {/* Question Screen */}
              <div className="flex-[3] flex flex-col justify-between bg-white border-r border-gray-200">
                <div className="p-5 overflow-y-auto">
                  <h3 className="font-bold text-gray-800 border-b pb-2 mb-4 text-sm">
                    {t[language].question} {currentQIndex + 1}:
                  </h3>
                  <p className="text-sm font-medium text-gray-800 mb-6">
                    {language === "en" ? currentQ.textEn : currentQ.textHi}
                  </p>

                  <div className="flex flex-col gap-3">
                    {(language === "en"
                      ? currentQ.optionsEn
                      : currentQ.optionsHi
                    ).map((opt, i) => (
                      <label
                        key={i}
                        className={`flex items-center gap-3 p-3 border rounded text-xs cursor-pointer transition ${selectedOptions[currentQIndex] === i ? "bg-blue-50 border-blue-500 font-semibold" : "hover:bg-gray-50"}`}
                      >
                        <input
                          type="radio"
                          name="opt"
                          checked={selectedOptions[currentQIndex] === i}
                          onChange={() => handleOptionSelect(i)}
                          className="accent-blue-600"
                        />
                        <span>{opt}</span>
                      </label>
                    ))}
                  </div>
                </div>

                {/* Question Actions */}
                <div className="bg-gray-50 p-3 border-t flex flex-col gap-2">
                  <div className="flex gap-2">
                    <button
                      onClick={() => updateStatusAndMove(2)}
                      className="bg-[#5cb85c] text-white px-3 py-1.5 text-xs font-bold rounded hover:bg-green-600"
                    >
                      {t[language].saveNext}
                    </button>
                    <button
                      onClick={() => {
                        const o = { ...selectedOptions };
                        delete o[currentQIndex];
                        setSelectedOptions(o);
                      }}
                      className="bg-white border px-3 py-1.5 text-xs font-bold rounded text-gray-700"
                    >
                      {t[language].clear}
                    </button>
                    <button
                      onClick={() => updateStatusAndMove(4)}
                      className="bg-[#f0ad4e] text-white px-3 py-1.5 text-xs font-bold rounded hover:bg-orange-500"
                    >
                      {t[language].saveMark}
                    </button>
                  </div>
                  <div className="flex justify-between border-t pt-2">
                    <button
                      onClick={() =>
                        currentQIndex > 0 && setCurrentQIndex(currentQIndex - 1)
                      }
                      disabled={currentQIndex === 0}
                      className="border bg-white px-3 py-1 text-xs font-bold rounded disabled:opacity-50"
                    >
                      {t[language].back}
                    </button>
                    <button
                      onClick={() =>
                        currentQIndex < TOTAL_QUESTIONS - 1 &&
                        setCurrentQIndex(currentQIndex + 1)
                      }
                      disabled={currentQIndex === TOTAL_QUESTIONS - 1}
                      className="border bg-white px-3 py-1 text-xs font-bold rounded disabled:opacity-50"
                    >
                      {t[language].next}
                    </button>
                  </div>
                </div>
              </div>

              {/* Minimal Palette */}
              <div className="flex-1 bg-[#f8fafc] p-3 flex flex-col border-l border-gray-200">
                <div className="text-center font-bold text-xs text-gray-700 pb-2 border-b">
                  {t[language].palette}
                </div>
                <div className="grid grid-cols-3 gap-2 py-3 overflow-y-auto flex-1 content-start">
                  {qStatus.map((st, i) => (
                    <button
                      key={i}
                      onClick={() => setCurrentQIndex(i)}
                      className={`h-8 w-full font-bold text-xs rounded flex items-center justify-center border ${st === 2 ? "bg-green-600 text-white" : st === 1 ? "bg-red-500 text-white" : "bg-gray-100 text-gray-700"}`}
                    >
                      {i + 1}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ================= RIGHT: PRAMAAN ADMIN COMMAND CENTER ================= */}
        {(viewMode === "admin" || viewMode === "split") && (
          <div className="flex-1 flex flex-col bg-slate-900 text-slate-100 overflow-y-auto p-4 gap-4">
            <div className="flex justify-between items-center border-b border-slate-700 pb-2">
              <div>
                <h2 className="text-base font-black text-white flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-green-500 animate-ping"></span>
                  PRAMAAN Command Center
                </h2>
                <p className="text-[11px] text-slate-400">
                  Resilience, Recovery & Audit Engine
                </p>
              </div>
              <div className="text-right">
                <span className="text-[10px] bg-slate-800 border border-slate-700 px-2 py-0.5 rounded text-orange-300 font-mono">
                  LIVE BACKEND DEMO
                </span>
              </div>
            </div>

            {/* Incident Detection Card — driven by GET /incidents?exam_id= */}
            <div
              className={`p-3 rounded-lg border shadow-lg ${isNodeFailed ? "bg-red-900/30 border-red-700" : "bg-emerald-900/30 border-emerald-700"}`}
            >
              <h3
                className={`text-xs font-bold mb-2 tracking-wider ${isNodeFailed ? "text-red-400" : "text-emerald-400"}`}
              >
                {isNodeFailed
                  ? "⚠️ INCIDENT DETECTED"
                  : "✅ INCIDENT STATUS"}
              </h3>
              {incidents.length === 0 ? (
                <ul className="text-[11px] space-y-1 text-slate-300">
                  <li className="text-emerald-300 font-bold">
                    • NO INCIDENTS RECORDED
                  </li>
                  <li className="text-slate-400 italic">
                    Node health{" "}
                    {nodeHealth?.health_state
                      ? `${nodeHealth.health_state} (${nodeHealth.reason})`
                      : "syncing…"}
                  </li>
                </ul>
              ) : (
                <ul className="text-[11px] space-y-1 text-slate-300">
                  {incidents.map((inc) => (
                    <li key={inc.id} className="leading-relaxed">
                      <span
                        className={
                          inc.status === "ACTIVE"
                            ? "text-red-300 font-bold"
                            : "text-emerald-300 font-bold"
                        }
                      >
                        • INC-{inc.id} · {inc.severity} · {inc.status}
                      </span>
                      <div className="text-slate-400 font-mono ml-3">
                        scope: {inc.affected_session_ids.length} session(s) [
                        {inc.affected_session_ids.join(", ") || "—"}]
                      </div>
                      {inc.root_cause_summary && (
                        <div className="text-slate-400 ml-3">{inc.root_cause_summary}</div>
                      )}
                      {inc.evidence_event_sequence_nos.length > 0 && (
                        <div className="mt-1 pt-1 border-t border-slate-700/60 text-slate-400 font-mono ml-3">
                          Evidence seq:{" "}
                          {inc.evidence_event_sequence_nos.join(", ")}
                        </div>
                      )}
                    </li>
                  ))}
                  <li className="text-slate-400 italic">
                    {incidents.filter((i) => i.status === "ACTIVE").length} active ·{" "}
                    {incidents.filter((i) => i.status !== "ACTIVE").length} resolved
                  </li>
                </ul>
              )}
            </div>

            {/* Step 1 & 2: Failure & Recovery Controls */}
            <div className="bg-slate-800/80 p-3 rounded-lg border border-slate-700 flex flex-col gap-2">
              <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                1. Resilience Injection (Live Test)
              </span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={handleInjectFailure}
                  disabled={isNodeFailed || busyAction !== null}
                  className="bg-red-600 hover:bg-red-700 disabled:opacity-40 text-white font-bold py-2 px-3 rounded text-xs flex items-center justify-center gap-1.5 shadow"
                >
                  {busyAction === "fail" ? "⏳ Injecting…" : "⚡ Inject Node Failure"}
                </button>
                <button
                  onClick={handleRecoverNode}
                  disabled={!isNodeFailed || busyAction !== null}
                  className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 text-white font-bold py-2 px-3 rounded text-xs flex items-center justify-center gap-1.5 shadow"
                >
                  {busyAction === "recover"
                    ? "⏳ Reconciling…"
                    : "🔄 Recover Node & Sync"}
                </button>
              </div>
            </div>

            {/* Reconciliation Proof — counts come from POST /session/{id}/reconcile */}
            {reconciliationLog && (
              <div className="bg-emerald-950/70 border border-emerald-500/50 p-3 rounded-lg">
                <div className="flex justify-between items-center text-xs font-bold text-emerald-400 border-b border-emerald-800/60 pb-1 mb-2">
                  <span>Reconciliation Proof</span>
                  <span className="bg-emerald-500 text-slate-950 px-2 py-0.5 rounded text-[10px] font-black">
                    RECONCILIATION: {reconciliationLog.status}
                  </span>
                </div>
                <div className="grid grid-cols-3 gap-2 text-[11px] font-mono text-emerald-200">
                  <div>
                    Local events:{" "}
                    <strong>{reconciliationLog.submitted}</strong>
                  </div>
                  <div>
                    Server events:{" "}
                    <strong>{reconciliationLog.acknowledged}</strong>
                  </div>
                  <div>
                    Newly reconciled:{" "}
                    <strong>{reconciliationLog.newly}</strong>
                  </div>
                  <div>
                    Missing IDs: <strong>{reconciliationLog.missing}</strong>
                  </div>
                  <div>
                    Mismatches: <strong>{reconciliationLog.mismatched}</strong>
                  </div>
                  <div>
                    Rejected: <strong>{reconciliationLog.rejected}</strong>
                  </div>
                  <div className="col-span-3 text-emerald-300">
                    Sessions reconciled:{" "}
                    <strong>
                      {reconciliationLog.sessionsReconciled}/
                      {reconciliationLog.sessionsAttempted}
                    </strong>
                  </div>
                </div>
                {reconciliationLog.perSession.some((r) => !r.ok) && (
                  <div className="mt-2 pt-2 border-t border-emerald-800/60 text-[11px] font-mono text-amber-300">
                    {reconciliationLog.perSession
                      .filter((r) => !r.ok)
                      .map((r) => `session ${r.session_id}: ${r.error}`)
                      .join(" · ")}
                  </div>
                )}
              </div>
            )}

            {/* AI Incident Analysis */}
            <div className="bg-slate-800/80 p-3 rounded-lg border border-slate-700 flex flex-col gap-2">
              <div className="flex justify-between items-center">
                <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                  2. Evidence-Grounded AI Analysis
                </span>
                <button
                  onClick={handleGenerateAiReport}
                  disabled={busyAction === "ai"}
                  className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-40 text-white px-2.5 py-1 rounded text-xs font-semibold"
                >
                  {busyAction === "ai" ? "Analyzing…" : "Generate Report"}
                </button>
              </div>
              {aiError && (
                <div className="bg-rose-950/60 border border-rose-600 text-rose-300 p-2 rounded text-[11px]">
                  {aiError}
                </div>
              )}
              {aiReport ? (
                <div className="bg-slate-950 p-2.5 rounded border border-indigo-900/50 text-[11px] text-slate-300 flex flex-col gap-1 leading-relaxed">
                  <div>
                    <strong className="text-indigo-400">Incident:</strong> INC-
                    {aiReport.incident_id}
                  </div>
                  <div>
                    <strong className="text-indigo-400">Likely Cause:</strong>{" "}
                    {aiReport.likely_cause}
                  </div>
                  <div>
                    <strong className="text-indigo-400">Impact:</strong>{" "}
                    {aiReport.impact_summary}
                  </div>
                  <div>
                    <strong className="text-indigo-400">Recommended Response:</strong>{" "}
                    {aiReport.recommended_response}
                  </div>
                  <div className="flex gap-1 items-center mt-1 flex-wrap">
                    <strong className="text-indigo-400">Evidence Refs:</strong>
                    {aiReport.evidence_refs.map((ref) => (
                      <span
                        key={ref}
                        className="bg-indigo-950 border border-indigo-600 text-indigo-300 px-1 rounded text-[10px] font-mono"
                      >
                        {ref}
                      </span>
                    ))}
                  </div>
                </div>
              ) : (
                !aiError && (
                  <div className="text-[11px] text-slate-500 italic">
                    No report generated yet. Click button after failure.
                  </div>
                )
              )}
            </div>

            {/* Audit & Cryptographic Tamper Detection */}
            <div className="bg-slate-800/80 p-3 rounded-lg border border-slate-700 flex flex-col gap-2.5">
              <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                3. Hash-Chained Audit Trail
              </span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={handleVerifyAudit}
                  disabled={busyAction === "audit"}
                  className="bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white font-bold py-1.5 px-3 rounded text-xs"
                >
                  {busyAction === "audit" ? "🛡️ Verifying…" : "🛡️ Verify Audit Chain"}
                </button>
                <button
                  onClick={handleSimulateTampering}
                  disabled={busyAction === "tamper"}
                  className="bg-amber-600 hover:bg-amber-700 disabled:opacity-40 text-white font-bold py-1.5 px-3 rounded text-xs"
                >
                  {busyAction === "tamper" ? "⚠️ Tampering…" : "⚠️ Simulate Tampering"}
                </button>
              </div>

              {tamperResult && (
                <div className="p-2.5 rounded border border-amber-500/50 bg-amber-950/60 text-[11px] text-amber-300 font-mono">
                  Tampered sequence_no:{" "}
                  <strong>{tamperResult.tampered_sequence_no}</strong> (field:{" "}
                  {tamperResult.field}) — {tamperResult.detail}
                </div>
              )}

              {auditResult && (
                <div
                  className={`p-2.5 rounded border text-xs flex flex-col gap-1 ${auditResult.valid ? "bg-emerald-950/60 border-emerald-500 text-emerald-300" : "bg-rose-950/70 border-rose-500 text-rose-300"}`}
                >
                  <div className="flex justify-between items-center font-bold font-mono">
                    <span>Audit Status:</span>
                    <span
                      className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider text-white"
                      style={{
                        background: auditResult.valid ? "#059669" : "#dc2626",
                      }}
                    >
                      {auditStatus}
                    </span>
                  </div>
                  <div className="text-[11px] font-mono">
                    Events checked: <strong>{auditResult.events_checked}</strong>
                  </div>
                  <div className="text-[11px] font-mono">
                    First broken sequence_no:{" "}
                    <strong>
                      {auditResult.first_broken_sequence_no ?? "none"}
                    </strong>
                  </div>
                  {auditResult.failure_reason && (
                    <div className="text-[11px] font-mono border-t border-rose-900/60 pt-1">
                      Failure reason: {auditResult.failure_reason}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
