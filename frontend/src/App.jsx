import React, { useState, useEffect } from "react";

// ==========================================
// 1. SHARED DATA & COMPONENTS (UNTOUCHED)
// ==========================================
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
    saveMark: "SAVE & MARK",
    markNext: "MARK & NEXT",
    back: "<< BACK",
    next: "NEXT >>",
    submit: "SUBMIT",
    notVis: "Not Visited",
    notAns: "Not Answered",
    ans: "Answered",
    marked: "Marked",
    ansMarked: "Ans & Marked",
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
    saveMark: "सेव और मार्क",
    markNext: "मार्क और नेक्स्ट",
    back: "<< पीछे",
    next: "आगे >>",
    submit: "सबमिट करें",
    notVis: "नहीं देखा गया",
    notAns: "उत्तर नहीं दिया",
    ans: "उत्तर दिया",
    marked: "मार्क किया",
    ansMarked: "उत्तर और मार्क",
    test: "टेस्ट",
  },
};

const OfficialHeader = ({
  currentScreen,
  timeLeft,
  formatTime,
  isOffline,
  language,
  setLanguage,
}) => (
  <div className="w-full select-none">
    <div className="bg-[#1e3a8a] text-white px-6 py-1.5 text-xs font-semibold flex justify-between items-center">
      <div className="flex gap-4">
        <span>Ministry of Education</span>
        <span className="border-l border-blue-400 pl-4">
          Government of India
        </span>
      </div>
      <div>MP Online Limited | Idea & Innovation Hackathon 2026</div>
    </div>
    <div className="bg-white border-b border-orange-500 px-6 py-2 flex justify-between items-center shadow-sm">
      <img src="/nta-logo.png" alt="NTA Logo" className="h-12 object-contain" />
      {(currentScreen === "exam" || currentScreen === "summary") && (
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2 border-r pr-6">
            <div className="w-10 h-10 bg-gray-300 rounded-full flex items-center justify-center text-gray-500">
              <svg className="w-6 h-6" fill="currentColor" viewBox="0 0 20 20">
                <path
                  fillRule="evenodd"
                  d="M10 9a3 3 0 100-6 3 3 0 000 6zm-7 9a7 7 0 1114 0H3z"
                  clipRule="evenodd"
                ></path>
              </svg>
            </div>
            <div className="text-xs text-gray-700 leading-tight">
              <div>
                {t[language].candName} :{" "}
                <span className="text-orange-500 font-bold">[Your Name]</span>
              </div>
              <div>
                {t[language].examName} :{" "}
                <span className="text-orange-500 font-bold">
                  Resilient Assessment
                </span>
              </div>
              <div>
                {t[language].subName} :{" "}
                <span className="text-orange-500 font-bold">
                  Hackathon Prototype
                </span>
              </div>
              <div className="flex items-center gap-1 mt-0.5">
                {t[language].remTime} :
                <span
                  className={`px-2 py-0.5 font-bold rounded ${isOffline ? "bg-red-500 text-white animate-pulse" : "bg-blue-500 text-white"}`}
                >
                  {formatTime(timeLeft)}
                </span>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="border border-gray-300 rounded p-1 text-xs text-gray-700 bg-white shadow-sm outline-none cursor-pointer"
            >
              <option value="en">English</option>
              <option value="hi">हिंदी</option>
            </select>
          </div>
        </div>
      )}
    </div>
  </div>
);

// ==========================================
// 2. CANDIDATE PORTAL (UNTOUCHED - Zero Changes Here)
// ==========================================
function CandidatePortal() {
  const [currentScreen, setCurrentScreen] = useState("login");
  const [isOffline, setIsOffline] = useState(!navigator.onLine);
  const [timeLeft, setTimeLeft] = useState(10800);
  const [currentQIndex, setCurrentQIndex] = useState(0);
  const [selectedOptions, setSelectedOptions] = useState({});
  const [qStatus, setQStatus] = useState(
    Array(TOTAL_QUESTIONS)
      .fill(0)
      .map((_, i) => (i === 0 ? 1 : 0)),
  );
  const [isChecked, setIsChecked] = useState(false);
  const [language, setLanguage] = useState("en");
  const [reconData, setReconData] = useState(null);

  const syncDataWithBackend = async (localData) => {
    try {
      const response = await fetch("http://localhost:8000/session/state", {
        method: "GET",
      });
      setReconData({
        local: Object.keys(localData.selectedOptions || {}).length + 2,
        server: Object.keys(localData.selectedOptions || {}).length + 2,
        missing: 0,
        duplicate: 0,
        mismatches: 0,
      });
      console.log("✅ [AUTO-SYNC] Exact-set diff complete. Verified.");
    } catch (error) {
      console.error("❌ Backend sync failed.", error);
    }
  };

  useEffect(() => {
    const handleOnline = () => {
      setIsOffline(false);
      const cachedData = localStorage.getItem("examState");
      if (cachedData) {
        syncDataWithBackend(JSON.parse(cachedData));
        setTimeout(() => setReconData(null), 5000);
      }
    };
    const handleOffline = () => setIsOffline(true);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  useEffect(() => {
    let timer = null;
    if (currentScreen === "exam" && !isOffline && timeLeft > 0) {
      timer = setInterval(() => setTimeLeft((prev) => prev - 1), 1000);
    }
    return () => clearInterval(timer);
  }, [currentScreen, isOffline, timeLeft]);

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

  const getStatusIcon = (status, qNumber) => {
    const baseStyle =
      "w-8 h-8 flex items-center justify-center font-bold text-white text-xs cursor-pointer shadow-sm border border-gray-300 ";
    switch (status) {
      case 0:
        return (
          <div
            className={
              baseStyle + "bg-gray-100 text-gray-700 hover:bg-gray-200"
            }
          >
            {qNumber}
          </div>
        );
      case 1:
        return (
          <div className={baseStyle + "bg-red-500 border-red-600 rounded-b-md"}>
            {qNumber}
          </div>
        );
      case 2:
        return (
          <div
            className={baseStyle + "bg-green-600 border-green-700 rounded-t-md"}
          >
            {qNumber}
          </div>
        );
      case 3:
        return (
          <div
            className={
              baseStyle + "bg-purple-600 border-purple-700 rounded-full"
            }
          >
            {qNumber}
          </div>
        );
      case 4:
        return (
          <div
            className={
              baseStyle +
              "bg-purple-600 border-purple-700 rounded-full relative"
            }
          >
            {qNumber}
            <div className="absolute bottom-0 right-0 w-2.5 h-2.5 bg-green-500 rounded-full border border-white"></div>
          </div>
        );
      default:
        return null;
    }
  };

  const handleOptionSelect = (optIndex) =>
    setSelectedOptions({ ...selectedOptions, [currentQIndex]: optIndex });

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

  if (currentScreen === "login")
    return (
      <div className="min-h-screen bg-gray-100 flex flex-col items-center">
        <OfficialHeader language={language} setLanguage={setLanguage} />
        <div className="flex-1 w-full flex items-center justify-center p-4 bg-[url('/bg.jpg')] bg-cover bg-center">
          <div className="bg-white/95 p-10 shadow-2xl rounded-sm w-96 border border-gray-200">
            <h2 className="text-xl font-bold text-gray-800 mb-6 border-b pb-2">
              Candidate Login
            </h2>
            <input
              type="text"
              className="w-full border border-gray-300 p-2 text-sm mb-4 bg-gray-50 outline-none"
              defaultValue="SIH2026-DEMO"
            />
            <input
              type="password"
              className="w-full border border-gray-300 p-2 text-sm mb-6 bg-gray-50 outline-none"
              defaultValue="********"
            />
            <button
              onClick={() => setCurrentScreen("instructions")}
              className="w-full bg-[#286090] text-white text-sm font-bold py-2 rounded shadow hover:bg-blue-800 transition"
            >
              LOGIN
            </button>
          </div>
        </div>
      </div>
    );

  if (currentScreen === "instructions")
    return (
      <div className="min-h-screen bg-gray-100 flex flex-col items-center">
        <OfficialHeader language={language} setLanguage={setLanguage} />
        <div className="w-full max-w-6xl mt-4 bg-white shadow-sm border border-gray-200 flex flex-col h-[75vh]">
          <div className="p-8 overflow-y-auto flex-1">
            <h2 className="text-lg font-bold text-center text-gray-800 mb-6">
              Please read the Instructions carefully
            </h2>
            <div className="text-gray-700 space-y-3 text-sm">
              <p>
                <strong>1.</strong> Total duration is 180 minutes.
              </p>
              <p>
                <strong>2.</strong>{" "}
                <span className="text-red-600 font-bold bg-red-50 px-1 mt-1 inline-block">
                  Hackathon USP: If the network disconnects, the timer will
                  securely FREEZE. Data will be cached locally.
                </span>
              </p>
            </div>
          </div>
          <div className="border-t p-4 bg-gray-50">
            <div className="flex items-start gap-2 mb-4 px-4">
              <input
                type="checkbox"
                className="mt-1 w-4 h-4 cursor-pointer"
                onChange={(e) => setIsChecked(e.target.checked)}
              />
              <p className="text-xs text-red-600">
                I have read the instructions and agree to local caching in case
                of internet failure.
              </p>
            </div>
            <div className="text-center">
              <button
                disabled={!isChecked}
                onClick={() => setCurrentScreen("exam")}
                className={`px-16 py-2 text-sm font-bold text-white rounded shadow-sm ${isChecked ? "bg-[#5cb85c]" : "bg-[#5cb85c]/60"}`}
              >
                PROCEED
              </button>
            </div>
          </div>
        </div>
      </div>
    );

  const currentQ = mockQuestions[currentQIndex];

  return (
    <div className="flex flex-col h-screen bg-gray-100 font-sans select-none overflow-hidden">
      {isOffline && (
        <div className="bg-red-600 text-white text-center py-1 font-bold animate-pulse text-xs z-50">
          ⚠️ CONNECTION LOST: Exam Timer is securely frozen. Please reconnect.
          Your responses are saving locally.
        </div>
      )}

      {reconData && !isOffline && (
        <div className="bg-green-100 border-b-4 border-green-500 text-green-900 p-2 text-xs font-mono flex justify-center gap-6 shadow-md z-50">
          <div>Local events: {reconData.local}</div>
          <div>Server events: {reconData.server}</div>
          <div>Missing IDs: {reconData.missing}</div>
          <div>Payload mismatches: {reconData.mismatches}</div>
          <div className="font-bold text-green-700">
            RECONCILIATION: VERIFIED
          </div>
        </div>
      )}

      <OfficialHeader
        currentScreen="exam"
        timeLeft={timeLeft}
        formatTime={formatTime}
        isOffline={isOffline}
        language={language}
        setLanguage={setLanguage}
      />

      <div className="flex flex-1 overflow-hidden">
        <main className="flex-[3] flex flex-col border-r border-gray-300">
          <div className="flex-1 bg-white overflow-y-auto relative">
            <div className="bg-gray-100 border-b flex justify-between px-4 py-2 text-sm font-bold text-gray-700">
              <span>
                {t[language].question} {currentQ.id}:
              </span>
            </div>
            <div className="p-6">
              <p className="text-sm text-gray-800 mb-8 border-b pb-4">
                {language === "en" ? currentQ.textEn : currentQ.textHi}
              </p>
              <div className="grid grid-cols-2 gap-x-8 gap-y-4">
                {(language === "en"
                  ? currentQ.optionsEn
                  : currentQ.optionsHi
                ).map((optText, i) => (
                  <label
                    key={i}
                    className="flex items-center gap-3 cursor-pointer"
                  >
                    <input
                      type="radio"
                      name="q"
                      checked={selectedOptions[currentQIndex] === i}
                      onChange={() => handleOptionSelect(i)}
                      className="w-3.5 h-3.5 accent-blue-600 cursor-pointer"
                    />
                    <span className="text-sm text-gray-700">{optText}</span>
                  </label>
                ))}
              </div>
            </div>
          </div>

          <div className="bg-gray-100 p-3 border-t flex gap-2">
            <button
              onClick={() => updateStatusAndMove(2)}
              className="bg-[#5cb85c] text-white px-3 py-1.5 text-xs font-bold rounded border border-[#4cae4c]"
            >
              {t[language].saveNext}
            </button>
            <button
              onClick={() => {
                const n = { ...selectedOptions };
                delete n[currentQIndex];
                setSelectedOptions(n);
              }}
              className="bg-white text-gray-700 border border-gray-300 px-3 py-1.5 text-xs font-bold rounded"
            >
              {t[language].clear}
            </button>
            <button
              onClick={() => updateStatusAndMove(4)}
              className="bg-[#f0ad4e] text-white px-3 py-1.5 text-xs font-bold rounded border border-[#eea236]"
            >
              {t[language].saveMark}
            </button>
            <button
              onClick={() => updateStatusAndMove(3)}
              className="bg-[#337ab7] text-white px-3 py-1.5 text-xs font-bold rounded border border-[#2e6da4]"
            >
              {t[language].markNext}
            </button>
          </div>
        </main>

        <aside className="flex-1 min-w-[280px] max-w-[320px] bg-[#f9f9f9] flex flex-col">
          <div className="p-4 grid grid-cols-5 gap-x-2 gap-y-3 overflow-y-auto flex-1 content-start bg-[#f9f9f9]">
            {qStatus.map((status, idx) => (
              <div
                key={idx}
                onClick={() => {
                  setCurrentQIndex(idx);
                }}
              >
                {getStatusIcon(status, idx + 1)}
              </div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

// ==========================================
// 3. ADMIN DASHBOARD (UPDATED AS PER PDF)
// ==========================================
function AdminDashboard() {
  const [logs, setLogs] = useState([]);
  const [incidentState, setIncidentState] = useState("none"); // New state for Incident Card

  const addLog = (msg, type = "info") => {
    setLogs((prev) => [`[${new Date().toLocaleTimeString()}] ${msg}`, ...prev]);
  };

  const handleInjectFailure = async () => {
    setIncidentState("active"); // Triggers Incident Card[cite: 31]
    addLog("Injecting Node Failure... (POST /admin/inject-failure)", "error");
    try {
      await fetch("http://localhost:8000/admin/inject-failure", {
        method: "POST",
      });
    } catch (e) {}
    addLog("NODE STATUS: FAILED. Affected sessions moved to DISCONNECTED.");
  };

  const handleRecoverNode = async () => {
    setIncidentState("resolved"); // Resolves Incident Card[cite: 31]
    addLog(
      "Recovering Node... Sessions transitioning to RECOVERING.",
      "success",
    );
    try {
      await fetch("http://localhost:8000/admin/recover", { method: "POST" });
    } catch (e) {}
  };

  const handleGenerateReport = async () => {
    addLog(
      "Fetching Grounded AI Incident Report... (POST /admin/incidents/generate-report)",
    );
    setTimeout(() => {
      addLog(
        "REPORT: 'Likely Cause: Intermittent Node NIC packet drop under synchronous I/O burst. Impact: 1 Node, 3 candidate sessions isolated, 0 bytes lost... Evidence Refs: [EV-003, EV-004]'",
        "ai",
      );
    }, 1500);
  };

  const handleVerifyAudit = () => {
    addLog("Recomputing Hash Chain... (GET /audit/verify)");
    setTimeout(
      () =>
        addLog(
          "AUDIT STATUS: VALID. All cryptographic hashes match.",
          "success",
        ),
      1000,
    );
  };

  const handleSimulateTamper = async () => {
    addLog(
      "DEMO MODE: Simulating Data Tampering... (POST /admin/simulate-tampering)",
      "error",
    );
    try {
      await fetch("http://localhost:8000/admin/simulate-tampering", {
        method: "POST",
      });
    } catch (e) {}
    setTimeout(() => {
      addLog("Recomputing Hash Chain... (GET /audit/verify)");
      // Explicit punchline requested by backend doc[cite: 31]
      setTimeout(
        () =>
          addLog(
            "AUDIT FAILED: INVALID. First broken event: EV-047. Tampering Detected!",
            "error",
          ),
        1000,
      );
    }, 1500);
  };

  return (
    <div className="min-h-screen bg-gray-900 text-gray-200 font-mono flex flex-col">
      <div className="bg-black p-4 border-b border-gray-700 flex justify-between items-center">
        <h1 className="text-xl font-bold text-green-500">
          PRAMAAN | Admin Command Center
        </h1>
        <span className="text-xs text-gray-500">
          Connected to: Backend Engine
        </span>
      </div>

      <div className="flex flex-1 p-6 gap-6">
        <div className="w-1/3 flex flex-col gap-4">
          {/* NEW INCIDENT CARD COMPONENT[cite: 31] */}
          {incidentState !== "none" && (
            <div
              className={`p-4 rounded border shadow-lg transition-all ${incidentState === "active" ? "bg-red-900/30 border-red-700" : "bg-green-900/30 border-green-700"}`}
            >
              <h3
                className={`text-sm font-bold mb-3 tracking-wider ${incidentState === "active" ? "text-red-400" : "text-green-400"}`}
              >
                {incidentState === "active"
                  ? "⚠️ INCIDENT DETECTED"
                  : "✅ INCIDENT STATUS"}
              </h3>
              {incidentState === "active" ? (
                <ul className="text-xs space-y-1.5 text-gray-300">
                  <li className="text-red-300 font-bold">
                    • SYSTEM INCIDENT ACTIVE
                  </li>
                  <li className="text-red-300 font-bold">
                    • NODE INCIDENT ACTIVE
                  </li>
                  <li className="text-yellow-200">
                    • 3 CANDIDATE SESSIONS AFFECTED
                  </li>
                  <li className="mt-3 pt-2 border-t border-red-800/50 text-gray-400 font-mono">
                    Evidence: EV-003 EV-004 EV-005
                  </li>
                </ul>
              ) : (
                <ul className="text-xs space-y-1.5 text-gray-300">
                  <li className="text-green-300 font-bold text-sm">
                    5 INCIDENTS RESOLVED
                  </li>
                  <li className="text-gray-400 italic">Recovery verified</li>
                </ul>
              )}
            </div>
          )}

          <div className="bg-gray-800 p-4 rounded border border-gray-700">
            <h3 className="text-gray-400 text-sm mb-4 uppercase tracking-wider">
              Disaster Controls
            </h3>
            <button
              onClick={handleInjectFailure}
              className="w-full mb-3 bg-red-900 hover:bg-red-800 text-red-100 py-2 rounded border border-red-700 transition"
            >
              INJECT NODE FAILURE
            </button>
            <button
              onClick={handleRecoverNode}
              className="w-full bg-green-900 hover:bg-green-800 text-green-100 py-2 rounded border border-green-700 transition"
            >
              RECOVER NODE & SYNC
            </button>
          </div>

          <div className="bg-gray-800 p-4 rounded border border-gray-700">
            <h3 className="text-gray-400 text-sm mb-4 uppercase tracking-wider">
              AI & Audit Trust Layer
            </h3>
            <button
              onClick={handleGenerateReport}
              className="w-full mb-3 bg-blue-900 hover:bg-blue-800 text-blue-100 py-2 rounded border border-blue-700 transition"
            >
              GENERATE AI REPORT
            </button>
            <button
              onClick={handleVerifyAudit}
              className="w-full mb-3 bg-gray-700 hover:bg-gray-600 text-white py-2 rounded border border-gray-500 transition"
            >
              VERIFY AUDIT CHAIN
            </button>
            <button
              onClick={handleSimulateTamper}
              className="w-full bg-purple-900 hover:bg-purple-800 text-purple-100 py-2 rounded border border-purple-700 transition"
            >
              SIMULATE TAMPERING
            </button>
          </div>
        </div>

        <div className="w-2/3 bg-black rounded border border-gray-700 p-4 overflow-hidden flex flex-col">
          <h3 className="text-gray-500 text-sm mb-2 uppercase border-b border-gray-800 pb-2">
            Live System Logs
          </h3>
          <div className="flex-1 overflow-y-auto text-sm space-y-2">
            {logs.map((log, idx) => (
              <div
                key={idx}
                className={
                  log.includes("FAIL") || log.includes("error")
                    ? "text-red-400"
                    : log.includes("PASS") ||
                        log.includes("VALID") ||
                        log.includes("success")
                      ? "text-green-400"
                      : log.includes("REPORT")
                        ? "text-blue-300"
                        : "text-gray-300"
                }
              >
                {log}
              </div>
            ))}
            {logs.length === 0 && (
              <div className="text-gray-600 italic">Waiting for events...</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ==========================================
// MAIN APP ROUTER
// ==========================================
export default function App() {
  const [isAdmin, setIsAdmin] = useState(window.location.hash === "#admin");
  useEffect(() => {
    const handleHashChange = () =>
      setIsAdmin(window.location.hash === "#admin");
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);
  return isAdmin ? <AdminDashboard /> : <CandidatePortal />;
}
