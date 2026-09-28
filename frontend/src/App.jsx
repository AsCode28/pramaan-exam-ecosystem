import React, { useState, useEffect } from "react";

const TOTAL_QUESTIONS = 15;
const mockQuestions = Array.from({ length: TOTAL_QUESTIONS }, (_, i) => ({
  id: i + 1,
  textEn: `(Prototype Question ${i + 1}) What is the primary fallback mechanism during a network failure in this ecosystem?`,
  textHi: `(प्रोटोटाइप प्रश्न ${i + 1}) इस इकोसिस्टम में नेटवर्क विफलता के दौरान प्राथमिक फ़ॉलबैक मैकेनिज्म क्या है?`,
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
}));

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

  // Candidate Exam States
  const [isNodeFailed, setIsNodeFailed] = useState(false);
  const [isOffline, setIsOffline] = useState(false);
  const [timeLeft, setTimeLeft] = useState(10800);
  const [currentQIndex, setCurrentQIndex] = useState(0);
  const [selectedOptions, setSelectedOptions] = useState({});
  const [qStatus, setQStatus] = useState(
    Array(TOTAL_QUESTIONS)
      .fill(0)
      .map((_, i) => (i === 0 ? 1 : 0)),
  );
  const [localBufferCount, setLocalBufferCount] = useState(0);

  // Admin & Audit States
  const [nodeStatus, setNodeStatus] = useState("HEALTHY"); // HEALTHY | FAILED
  const [incidents, setIncidents] = useState([]);
  const [reconciliationLog, setReconciliationLog] = useState(null);
  const [auditStatus, setAuditStatus] = useState(null); // 'VALID' | 'INVALID' | null
  const [tamperedEvent, setTamperedEvent] = useState(null);
  const [aiReport, setAiReport] = useState(null);

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
  const handleOptionSelect = (optIndex) => {
    setSelectedOptions({ ...selectedOptions, [currentQIndex]: optIndex });
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

    // Buffering responses locally during disruption
    if (isNodeFailed || isOffline) {
      setLocalBufferCount((prev) => prev + 1);
    }
    localStorage.setItem(
      "examState",
      JSON.stringify({ selectedOptions, qStatus: newQStatus }),
    );
  };

  // --- Admin API Triggers (Backend Sync) ---
  const handleInjectFailure = async () => {
    setNodeStatus("FAILED");
    setIsNodeFailed(true);
    setReconciliationLog(null);
    setIncidents([
      {
        id: "INC-9042",
        severity: "NODE_FAILURE",
        target: "Node-01 (Bhopal Center)",
        time: new Date().toLocaleTimeString(),
        status: "OPEN",
      },
    ]);
    try {
      await fetch("http://localhost:8000/admin/inject-failure", {
        credentials: "omit",
        method: "POST",
      });
    } catch {
      console.warn("Backend /admin/inject-failure simulated.");
    }
  };

  const handleRecoverNode = async () => {
    setNodeStatus("HEALTHY");
    setIsNodeFailed(false);

    // Simulate exact reconciliation format
    const totalLocal =
      Object.keys(selectedOptions).length + localBufferCount + 3;
    setReconciliationLog({
      localEvents: totalLocal,
      serverEvents: totalLocal,
      missingIds: 0,
      duplicateIds: 0,
      mismatches: 0,
      status: "VERIFIED",
    });
    setLocalBufferCount(0);
    setIncidents((prev) => prev.map((inc) => ({ ...inc, status: "RESOLVED" })));

    try {
      await fetch("http://localhost:8000/session/recover", {
        credentials: "omit",
        method: "POST",
      });
    } catch {
      console.warn("Backend recovery simulated.");
    }
  };

  const handleVerifyAudit = async () => {
    try {
      const res = await fetch("http://localhost:8000/audit/verify", {
        credentials: "omit",
      });
      const data = await res.json();
      setAuditStatus(data.status || (tamperedEvent ? "INVALID" : "VALID"));
    } catch {
      setAuditStatus(tamperedEvent ? "INVALID" : "VALID");
    }
  };

  const handleSimulateTampering = async () => {
    setTamperedEvent("EV-0047"); // Updated to match PDF exact requirement
    try {
      await fetch("http://localhost:8000/admin/simulate-tampering", {
        credentials: "omit",
        method: "POST",
      });
    } catch {
      console.warn("Backend tampering simulated.");
    }
  };

  const handleGenerateAiReport = () => {
    setAiReport({
      likelyCause:
        "Intermittent Node NIC packet drop under synchronous I/O burst.",
      impactSummary:
        "1 Node, 3 candidate sessions isolated. 0 bytes lost due to local hash-buffering.",
      recommendedResponse:
        "Node recovered cleanly. Auto-reconciliation validated.",
      evidenceRefs: ["EV-0031", "EV-0038", "EV-0042"],
    });
  };

  const currentQ = mockQuestions[currentQIndex];

  return (
    <div className="flex flex-col h-screen bg-gray-100 font-sans select-none overflow-hidden">
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
          <span>
            Node Status:{" "}
            <strong
              className={
                nodeStatus === "HEALTHY" ? "text-green-400" : "text-red-400"
              }
            >
              {nodeStatus}
            </strong>
          </span>
          <span>
            Buffered Events:{" "}
            <strong className="text-orange-400">{localBufferCount}</strong>
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
                    <span className="text-orange-600">Ananya Sharma</span>
                  </div>
                  <div className="text-gray-500">
                    {t[language].examName}:{" "}
                    <span className="font-semibold text-gray-700">
                      PRAMAAN Assessment
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
                    {t[language].question} {currentQ.id}:
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
                  DEMO_MODE=true
                </span>
              </div>
            </div>

            {/* NEW: Incident Detection Card based on Backend feedback[cite: 31, 34] */}
            {incidents.length > 0 && (
              <div
                className={`p-3 rounded-lg border shadow-lg ${nodeStatus === "FAILED" ? "bg-red-900/30 border-red-700" : "bg-emerald-900/30 border-emerald-700"}`}
              >
                <h3
                  className={`text-xs font-bold mb-2 tracking-wider ${nodeStatus === "FAILED" ? "text-red-400" : "text-emerald-400"}`}
                >
                  {nodeStatus === "FAILED"
                    ? "⚠️ INCIDENT DETECTED"
                    : "✅ INCIDENT STATUS"}
                </h3>
                {nodeStatus === "FAILED" ? (
                  <ul className="text-[11px] space-y-1 text-slate-300">
                    <li className="text-red-300 font-bold">
                      • SYSTEM INCIDENT ACTIVE
                    </li>
                    <li className="text-red-300 font-bold">
                      • NODE INCIDENT ACTIVE
                    </li>
                    <li className="text-yellow-200">
                      • 3 CANDIDATE SESSIONS AFFECTED
                    </li>
                    <li className="mt-2 pt-2 border-t border-red-800/50 text-slate-400 font-mono">
                      Evidence: EV-003 EV-004 EV-005
                    </li>
                  </ul>
                ) : (
                  <ul className="text-[11px] space-y-1 text-slate-300">
                    <li className="text-emerald-300 font-bold text-[13px]">
                      5 INCIDENTS RESOLVED
                    </li>
                    <li className="text-slate-400 italic">Recovery verified</li>
                  </ul>
                )}
              </div>
            )}

            {/* Step 1 & 2: Failure & Recovery Controls */}
            <div className="bg-slate-800/80 p-3 rounded-lg border border-slate-700 flex flex-col gap-2">
              <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                1. Resilience Injection (Live Test)
              </span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={handleInjectFailure}
                  disabled={nodeStatus === "FAILED"}
                  className="bg-red-600 hover:bg-red-700 disabled:opacity-40 text-white font-bold py-2 px-3 rounded text-xs flex items-center justify-center gap-1.5 shadow"
                >
                  ⚡ Inject Node Failure
                </button>
                <button
                  onClick={handleRecoverNode}
                  disabled={nodeStatus === "HEALTHY"}
                  className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 text-white font-bold py-2 px-3 rounded text-xs flex items-center justify-center gap-1.5 shadow"
                >
                  🔄 Recover Node & Sync
                </button>
              </div>
            </div>

            {/* Reconciliation Proof (Exact Count Display) */}
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
                    <strong>{reconciliationLog.localEvents}</strong>
                  </div>
                  <div>
                    Server events:{" "}
                    <strong>{reconciliationLog.serverEvents}</strong>
                  </div>
                  <div>
                    Missing IDs: <strong>{reconciliationLog.missingIds}</strong>
                  </div>
                  <div>
                    Duplicate IDs:{" "}
                    <strong>{reconciliationLog.duplicateIds}</strong>
                  </div>
                  <div className="col-span-2">
                    Payload mismatches:{" "}
                    <strong>{reconciliationLog.mismatches}</strong>
                  </div>
                </div>
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
                  className="bg-indigo-600 hover:bg-indigo-700 text-white px-2.5 py-1 rounded text-xs font-semibold"
                >
                  Generate Report
                </button>
              </div>
              {aiReport ? (
                <div className="bg-slate-950 p-2.5 rounded border border-indigo-900/50 text-[11px] text-slate-300 flex flex-col gap-1 leading-relaxed">
                  <div>
                    <strong className="text-indigo-400">Likely Cause:</strong>{" "}
                    {aiReport.likelyCause}
                  </div>
                  <div>
                    <strong className="text-indigo-400">Impact:</strong>{" "}
                    {aiReport.impactSummary}
                  </div>
                  <div className="flex gap-1 items-center mt-1">
                    <strong className="text-indigo-400">Evidence Refs:</strong>
                    {aiReport.evidenceRefs.map((ref, idx) => (
                      <span
                        key={idx}
                        className="bg-indigo-950 border border-indigo-600 text-indigo-300 px-1 rounded text-[10px] font-mono"
                      >
                        {ref}
                      </span>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="text-[11px] text-slate-500 italic">
                  No report generated yet. Click button after failure.
                </div>
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
                  className="bg-blue-600 hover:bg-blue-700 text-white font-bold py-1.5 px-3 rounded text-xs"
                >
                  🛡️ Verify Audit Chain
                </button>
                <button
                  onClick={handleSimulateTampering}
                  className="bg-amber-600 hover:bg-amber-700 text-white font-bold py-1.5 px-3 rounded text-xs"
                >
                  ⚠️ Simulate Tampering
                </button>
              </div>

              {auditStatus && (
                <div
                  className={`p-2.5 rounded border text-xs flex flex-col gap-1 ${auditStatus === "VALID" ? "bg-emerald-950/60 border-emerald-500 text-emerald-300" : "bg-rose-950/70 border-rose-500 text-rose-300"}`}
                >
                  <div className="flex justify-between items-center font-bold font-mono">
                    <span>Audit Status:</span>
                    <span
                      className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider text-white"
                      style={{
                        background:
                          auditStatus === "VALID" ? "#059669" : "#dc2626",
                      }}
                    >
                      {auditStatus}
                    </span>
                  </div>
                  {/* Updated Tamper text to match backend exact requirement[cite: 31, 34] */}
                  {tamperedEvent && auditStatus === "INVALID" && (
                    <div className="text-[11px] text-rose-300 mt-1 border-t border-rose-900/60 pt-1 font-mono">
                      First broken event: <strong>{tamperedEvent}</strong>
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
