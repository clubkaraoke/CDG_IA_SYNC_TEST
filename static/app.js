const $ = (id) => document.getElementById(id);

const audioInput = $("audio");
const masterLyrics = $("masterLyrics");
const lyricsMeta = $("lyricsMeta");
const baseSelectBtn = $("baseSelectBtn");
const scribeSelectBtn = $("scribeSelectBtn");
const runSelectedBtn = $("runSelectedBtn");
const syncBtn = $("syncBtn");
const scribeBtn = $("scribeBtn");
const demoBtn = $("demoBtn");
const restoreAiBtn = $("restoreAiBtn");
const engineBadge = $("engineBadge");
const aiProgress = $("aiProgress");
const aiProgressBar = $("aiProgressBar");
const aiStatus = $("aiStatus");
const aiMeta = $("aiMeta");
const jobMetricsCard = $("jobMetricsCard");
const jobMetricsGrid = $("jobMetricsGrid");
const jobQualityBadge = $("jobQualityBadge");
const scribePanel = $("scribePanel");
const scribeTranscript = $("scribeTranscript");
const scribeCompareBadge = $("scribeCompareBadge");
const scribeCompareMeta = $("scribeCompareMeta");
const scribeEngine = $("scribeEngine");
const scribeEngineStatus = $("scribeEngineStatus");
const toggleJsonBtn = $("toggleJsonBtn");
const jsonPanel = $("jsonPanel");
const timingJson = $("timingJson");
const applyJsonBtn = $("applyJsonBtn");
const clearBtn = $("clearBtn");
const player = $("player");
const playBtn = $("playBtn");
const backBtn = $("backBtn");
const fwdBtn = $("fwdBtn");
const seek = $("seek");
const timeNow = $("timeNow");
const timeTotal = $("timeTotal");
const previewStatus = $("previewStatus");
const timingSourceBadge = $("timingSourceBadge");
const refreshLogBtn = $("refreshLogBtn");
const copyLogBtn = $("copyLogBtn");
const diagLog = $("diagLog");
const pv = $("pv");
const pvx = pv.getContext("2d");
const pvInfo = $("pvInfo");
const timingsBody = $("timingsBody");
const wordCountBadge = $("wordCountBadge");
const errorCard = $("errorCard");
const errorText = $("errorText");

const state = {
  objectUrl: null,
  lines: [],
  words: [],
  timingsReady: false,
  linesPerPage: 6,
  fontSize: 18,
  activeWord: -1,
  timingSource: "none",
  clientLogs: [],
  lastRawResult: null,
  lastAiResult: null,
  lastApplyWarnings: [],
  lastMetrics: null,
  lastEngineSource: null,
  selectedEngine: "base",
};

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v));
}

function showError(message) {
  errorText.textContent = message;
  errorCard.classList.remove("hidden");
}

function updateEngineSelector() {
  if (!baseSelectBtn || !scribeSelectBtn || !runSelectedBtn) return;

  const baseSelected = state.selectedEngine === "base";

  baseSelectBtn.classList.toggle("secondary", !baseSelected);
  scribeSelectBtn.classList.toggle("secondary", baseSelected);

  baseSelectBtn.textContent = baseSelected ? "🤖 BASE v2 local ✓" : "🤖 BASE v2 local";
  scribeSelectBtn.textContent = baseSelected ? "✨ ElevenLabs Scribe v2" : "✨ ElevenLabs Scribe v2 ✓";

  runSelectedBtn.textContent = baseSelected
    ? "▶ Procesar con BASE v2"
    : "▶ Procesar con Scribe v2";
}

function selectEngine(engine) {
  state.selectedEngine = engine === "scribe" ? "scribe" : "base";
  updateEngineSelector();
  logClient("info", "engine_selected", state.selectedEngine === "scribe"
    ? "Se seleccionó ElevenLabs Scribe v2."
    : "Se seleccionó BASE v2 local.");
}

function clearError() {
  errorCard.classList.add("hidden");
}

function logClient(level, event, message, details = null) {
  const row = {
    ts: new Date().toISOString(),
    source: "browser",
    level,
    event,
    message,
    details,
  };
  state.clientLogs.push(row);
  if (state.clientLogs.length > 300) state.clientLogs.shift();
  renderDiagnosticLog();
}

function setTimingSource(source, detail = "") {
  state.timingSource = source;
  timingSourceBadge.classList.remove("ok", "warn");
  if (source === "ai") {
    timingSourceBadge.textContent = detail ? `FUENTE: IA REAL · ${detail}` : "FUENTE: IA REAL";
    timingSourceBadge.classList.add("ok");
  } else if (source === "scribe") {
    timingSourceBadge.textContent = detail ? `FUENTE: SCRIBE V2 · ${detail}` : "FUENTE: SCRIBE V2";
    timingSourceBadge.classList.add("ok");
  } else if (source === "demo") {
    timingSourceBadge.textContent = "FUENTE: DEMO";
    timingSourceBadge.classList.add("warn");
  } else if (source === "json") {
    timingSourceBadge.textContent = "FUENTE: JSON MANUAL";
    timingSourceBadge.classList.add("warn");
  } else if (source === "ai-error") {
    timingSourceBadge.textContent = "FUENTE: IA REAL · ERROR AL APLICAR";
    timingSourceBadge.classList.add("warn");
  } else {
    timingSourceBadge.textContent = "FUENTE: NINGUNA";
  }
}

async function fetchServerLogs() {
  try {
    const payload = await apiJson("api/logs?limit=120");
    return Array.isArray(payload.events) ? payload.events : [];
  } catch (e) {
    return [{
      ts: new Date().toISOString(),
      source: "browser",
      level: "error",
      event: "server_logs_unavailable",
      message: e.message || String(e),
    }];
  }
}

async function renderDiagnosticLog() {
  if (!diagLog) return;
  const server = await fetchServerLogs();
  const combined = [
    ...server.map(r => ({ source: "ovh", ...r })),
    ...state.clientLogs,
  ].sort((a, b) => String(a.ts || "").localeCompare(String(b.ts || "")));

  const lines = combined.slice(-180).map((r) => {
    const details = r.details ? " " + JSON.stringify(r.details) : "";
    return `[${r.ts || "?"}] [${String(r.source || "?").toUpperCase()}] [${String(r.level || "info").toUpperCase()}] ${r.event || "event"} :: ${r.message || ""}${details}`;
  });
  diagLog.textContent = lines.length ? lines.join("\n") : "Sin eventos todavía.";
}

async function copyDiagnosticLog() {
  await renderDiagnosticLog();
  try {
    await navigator.clipboard.writeText(diagLog.textContent || "");
    logClient("info", "log_copied", "Log copiado al portapapeles.");
  } catch (e) {
    showError("No se pudo copiar el log: " + (e.message || e));
  }
}


async function apiJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { detail: `HTTP ${response.status}` };
  }
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

async function checkLocalEngine() {
  try {
    const h = await apiJson("api/local/health");
    const w = h.worker || {};
    engineBadge.textContent = `${w.whisper_model || "Whisper"} · CPU INT8 ✓`;
    engineBadge.classList.add("ok");
    syncBtn.disabled = false;
    if (baseSelectBtn) baseSelectBtn.disabled = false;
  } catch (e) {
    engineBadge.textContent = "Motor IA no disponible";
    engineBadge.classList.remove("ok");
    syncBtn.disabled = true;
    if (baseSelectBtn) baseSelectBtn.disabled = true;
  }
  updateEngineSelector();
}

async function checkElevenLabsEngine() {
  if (!scribeBtn) return;
  try {
    const h = await apiJson("api/elevenlabs/health");
    const configured = Boolean(h.configured);
    scribeBtn.disabled = !configured;
    if (scribeSelectBtn) scribeSelectBtn.disabled = !configured;
    if (scribeEngine) scribeEngine.classList.toggle("ready", configured);
    if (scribeEngine) scribeEngine.classList.toggle("planned", !configured);
    if (scribeEngineStatus) {
      scribeEngineStatus.textContent = configured
        ? `LISTO · ${h.model || "scribe_v2"} · timestamps por palabra`
        : "Falta ELEVENLABS_API_KEY en OVH";
    }
  } catch (e) {
    scribeBtn.disabled = true;
    if (scribeSelectBtn) scribeSelectBtn.disabled = true;
    if (scribeEngineStatus) scribeEngineStatus.textContent = "API no disponible";
  }
  updateEngineSelector();
}

function fmtTime(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const min = Math.floor(s / 60);
  const rem = s - min * 60;
  return `${String(min).padStart(2, "0")}:${rem.toFixed(3).padStart(6, "0")}`;
}

function fmtSec(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  return Number(v).toFixed(3) + " s";
}

function buildLyricsFromScribeWords(words) {
  const rows = [];
  let current = [];
  let prevEnd = null;

  (Array.isArray(words) ? words : []).forEach((w) => {
    const text = String(w?.text || "").trim();
    if (!text) return;

    const start = Number(w?.start);
    const end = Number(w?.end);
    const gap = Number.isFinite(start) && Number.isFinite(prevEnd) ? start - prevEnd : 0;

    if (current.length && (gap > 1.2 || current.length >= 9)) {
      rows.push(current.join(" "));
      current = [];
    }

    current.push(text);
    prevEnd = Number.isFinite(end) ? end : prevEnd;

    if (/[.!?…]["')\]]?$/.test(text) && current.length >= 3) {
      rows.push(current.join(" "));
      current = [];
    }
  });

  if (current.length) rows.push(current.join(" "));
  return rows.join("\n");
}

function qaLabel(status) {
  if (status === "green") return "🟢 VERDE";
  if (status === "yellow") return "🟡 AMARILLO";
  if (status === "red") return "🔴 ROJO";
  return "—";
}

function renderJobMetrics(result) {
  if (!jobMetricsCard || !jobMetricsGrid || !jobQualityBadge) return;
  const m = result?.metrics || {};
  const a = result?.rough_asr || {};
  const mask = result?.vocal_mask || {};
  const counts = m.line_status_counts || {};
  const totalLines = (counts.green || 0) + (counts.yellow || 0) + (counts.red || 0);

  state.lastMetrics = m;
  jobMetricsCard.classList.remove("hidden");
  jobQualityBadge.classList.remove("ok", "warn", "bad");

  if (result?.engine === "elevenlabs-scribe-v2") {
    const coverage = Number(m.coverage_ratio || 0);
    if (coverage >= 0.97 && Number(m.interpolated_words || 0) <= 3) {
      jobQualityBadge.textContent = `🟢 Cobertura ${Math.round(coverage * 100)}%`;
      jobQualityBadge.classList.add("ok");
    } else if (coverage >= 0.90) {
      jobQualityBadge.textContent = `🟡 Cobertura ${Math.round(coverage * 100)}%`;
      jobQualityBadge.classList.add("warn");
    } else {
      jobQualityBadge.textContent = `🔴 Cobertura ${Math.round(coverage * 100)}%`;
      jobQualityBadge.classList.add("bad");
    }

    const cells = [
      ["Tiempo API", result?.elapsed_s != null ? `${Number(result.elapsed_s).toFixed(2)} s` : "—"],
      ["Palabras Scribe", m.scribe_word_count ?? result?.scribe?.word_count ?? "—"],
      ["Palabras maestra", m.master_word_count ?? result?.master_word_count ?? "—"],
      ["Mapeadas", m.mapped_words ?? "—"],
      ["Cobertura", `${Math.round(coverage * 100)}%`],
      ["Exactas", m.exact_matches ?? 0],
      ["Fuzzy", m.fuzzy_matches ?? 0],
      ["Agrupadas 2↔1", m.grouped_matches ?? 0],
      ["Interpoladas", m.interpolated_words ?? 0],
      ["Extras Scribe", m.ignored_scribe_words ?? 0],
      ["Modelo", result?.scribe?.model_id || "scribe_v2"],
      ["Idioma", result?.scribe?.language_code || "—"],
    ];

    jobMetricsGrid.innerHTML = cells.map(([k, v]) => `
      <div class="metric-cell">
        <span>${escapeHtml(String(k))}</span>
        <strong>${escapeHtml(String(v))}</strong>
      </div>
    `).join("");
    return;
  }

  if ((counts.red || 0) > 0) {
    jobQualityBadge.textContent = `🔴 ${counts.red} línea(s) roja(s)`;
    jobQualityBadge.classList.add("bad");
  } else if ((counts.yellow || 0) > 0) {
    jobQualityBadge.textContent = `🟡 ${counts.yellow} línea(s) a revisar`;
    jobQualityBadge.classList.add("warn");
  } else if (totalLines) {
    jobQualityBadge.textContent = "🟢 Todo confiable";
    jobQualityBadge.classList.add("ok");
  } else {
    jobQualityBadge.textContent = "Sin QA";
  }

  const cells = [
    ["Tiempo", result?.elapsed_s != null ? `${Number(result.elapsed_s).toFixed(2)} s` : "—"],
    ["RAM pico", m.peak_rss_gb != null ? `${m.peak_rss_gb} GB` : "—"],
    ["CPU pico", m.peak_cpu_pct != null ? `${m.peak_cpu_pct}%` : "—"],
    ["Palabras", result?.master_word_count ?? "—"],
    ["Anchors", a.master_anchor_count != null ? `${a.master_anchor_count} · ${Math.round((a.master_anchor_ratio || 0) * 100)}%` : "—"],
    ["Anchors descartados", m.anchors_rejected ?? 0],
    ["Conf < 0.20", m.low_confidence_lt_020 ?? 0],
    ["Conf < 0.10", m.low_confidence_lt_010 ?? 0],
    ["Huecos sospechosos", m.suspicious_gaps ?? 0],
    ["Huecos extremos", m.extreme_gaps ?? 0],
    ["Líneas verdes", counts.green ?? 0],
    ["Líneas amarillas", counts.yellow ?? 0],
    ["Líneas rojas", counts.red ?? 0],
    ["Máscara silenciosa", mask.silent_ratio != null ? `${Math.round(mask.silent_ratio * 100)}%` : "—"],
  ];

  jobMetricsGrid.innerHTML = cells.map(([k, v]) => `
    <div class="metric-cell">
      <span>${escapeHtml(String(k))}</span>
      <strong>${escapeHtml(String(v))}</strong>
    </div>
  `).join("");
}

function parseMasterLyrics() {
  const rawLines = masterLyrics.value.replace(/\r/g, "").split("\n");
  const lines = [];
  const words = [];

  rawLines.forEach((raw, rawIndex) => {
    const trimmed = raw.trim();
    if (!trimmed) return;
    const parts = trimmed.match(/\S+/g) || [];
    const line = {
      index: lines.length,
      sourceIndex: rawIndex,
      text: trimmed,
      words: [],
    };
    parts.forEach((text) => {
      const word = {
        index: words.length,
        lineIndex: line.index,
        text,
        start: null,
        end: null,
      };
      line.words.push(word);
      words.push(word);
    });
    lines.push(line);
  });

  state.lines = lines;
  state.words = words;
  state.timingsReady = words.length > 0 && words.every(w => w.start !== null && w.end !== null);
  lyricsMeta.textContent = `${lines.length} línea${lines.length === 1 ? "" : "s"} · ${words.length} palabra${words.length === 1 ? "" : "s"}`;
  renderTable();
  updatePreviewStatus();
  drawPreview();
}

function clearTimings() {
  state.words.forEach(w => {
    w.start = null;
    w.end = null;
  });
  state.timingsReady = false;
  state.activeWord = -1;
  state.lastRawResult = null;
  state.lastAiResult = null;
  state.lastApplyWarnings = [];
  state.lastMetrics = null;
  state.lastEngineSource = null;
  timingJson.value = "";
  if (jobMetricsCard) jobMetricsCard.classList.add("hidden");
  if (jobMetricsGrid) jobMetricsGrid.innerHTML = "";
  if (scribePanel) scribePanel.classList.add("hidden");
  if (scribeTranscript) scribeTranscript.value = "";
  if (scribeCompareMeta) scribeCompareMeta.textContent = "";
  if (scribeCompareBadge) {
    scribeCompareBadge.textContent = "Sin prueba";
    scribeCompareBadge.classList.remove("ok", "warn", "bad");
  }
  if (jobQualityBadge) {
    jobQualityBadge.textContent = "Sin resultado";
    jobQualityBadge.classList.remove("ok", "warn", "bad");
  }
  setTimingSource("none");
  renderTable();
  updatePreviewStatus();
  drawPreview();
}

function updatePreviewStatus() {
  const count = state.words.filter(w => w.start !== null && w.end !== null).length;
  wordCountBadge.textContent = `${count}/${state.words.length} palabras`;
  if (state.timingsReady) {
    previewStatus.textContent = "Timings listos ✓";
    previewStatus.classList.add("ok");
  } else if (count) {
    previewStatus.textContent = `${count} timings parciales`;
    previewStatus.classList.remove("ok");
  } else {
    previewStatus.textContent = "Sin timings";
    previewStatus.classList.remove("ok");
  }
}

function applyTimingArray(inputWords, options = {}) {
  parseMasterLyrics();
  clearError();

  if (!state.words.length) {
    throw new Error("Pega primero la letra maestra exacta.");
  }
  if (!Array.isArray(inputWords)) {
    throw new Error('El JSON debe contener un arreglo "words".');
  }
  if (inputWords.length !== state.words.length) {
    throw new Error(`La letra maestra tiene ${state.words.length} palabras, pero el JSON trae ${inputWords.length}. No aplicaré timings incompletos para evitar desplazar palabras.`);
  }

  const warnings = [];
  let lastStart = -1;

  inputWords.forEach((tw, i) => {
    const start = Number(tw.start);
    const end = Number(tw.end);

    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      throw new Error(`Timing inválido en palabra #${i + 1} (${state.words[i].text}).`);
    }
    if (start < 0 || end < start) {
      throw new Error(`Rango inválido en palabra #${i + 1} (${state.words[i].text}): ${start} → ${end}.`);
    }

    if (start < lastStart) {
      warnings.push({
        index: i,
        word: state.words[i].text,
        previous_start: lastStart,
        start,
        type: "non_monotonic_start",
      });
    }
    lastStart = Math.max(lastStart, start);
  });

  inputWords.forEach((tw, i) => {
    state.words[i].start = Number(tw.start);
    state.words[i].end = Number(tw.end);
    state.words[i].confidence = tw.confidence ?? null;
    state.words[i].interpolated = Boolean(tw.interpolated);
    state.words[i].vocalSupport = tw.vocal_support ?? null;
    state.words[i].qaStatus = tw.qa_status ?? null;
    state.words[i].qaScore = tw.qa_score ?? null;
  });

  state.timingsReady = true;
  state.lastApplyWarnings = warnings;

  if (options.source) setTimingSource(options.source, warnings.length ? `${warnings.length} warning(s)` : "");

  renderTable();
  updatePreviewStatus();
  drawPreview();

  if (warnings.length) {
    logClient(
      "warn",
      "timings_applied_with_warnings",
      `Se aplicaron ${inputWords.length} palabras con ${warnings.length} anomalías de orden. El resultado IA NO fue descartado.`,
      { warnings: warnings.slice(0, 20) }
    );
  } else {
    logClient(
      "info",
      "timings_applied",
      `Se aplicaron ${inputWords.length} palabras correctamente.`,
      { source: options.source || "unknown" }
    );
  }

  return { warnings };
}

function generateDemoTimings() {
  clearError();

  if (state.lastAiResult) {
    const ok = window.confirm("Ya tienes un resultado de IA REAL. Timing demo reemplazará temporalmente el preview. Podrás volver con “Restaurar IA real”. ¿Continuar?");
    if (!ok) {
      logClient("info", "demo_cancelled", "Se canceló Timing demo para conservar IA REAL.");
      return;
    }
  }

  parseMasterLyrics();

  if (!audioInput.files.length) {
    return showError("Selecciona primero un audio de prueba.");
  }
  if (!state.words.length) {
    return showError("Pega primero la letra maestra exacta.");
  }

  const duration = Number(player.duration);
  if (!Number.isFinite(duration) || duration <= 1) {
    return showError("El audio todavía no terminó de cargar sus metadatos.");
  }

  const startAt = Math.min(8, Math.max(2, duration * 0.05));
  const endAt = Math.max(startAt + 1, duration - Math.min(8, duration * 0.06));
  const usable = Math.max(1, endAt - startAt);

  const units = [];
  let totalUnits = 0;
  state.lines.forEach((line, li) => {
    line.words.forEach((w, wi) => {
      const durU = clamp(0.42 + w.text.length * 0.035, 0.45, 0.95);
      const gapU = wi === line.words.length - 1 ? 0.34 : 0.09;
      units.push({ index: w.index, durU, gapU });
      totalUnits += durU + gapU;
    });
    if (li === state.lines.length - 1 && units.length) {
      totalUnits -= units[units.length - 1].gapU;
      units[units.length - 1].gapU = 0;
    }
  });

  const scale = usable / Math.max(totalUnits, 0.001);
  let t = startAt;
  const result = units.map((u) => {
    const start = t;
    const end = start + u.durU * scale;
    t = end + u.gapU * scale;
    return { text: state.words[u.index].text, start, end };
  });

  const payload = { engine: "demo", words: result };
  state.lastRawResult = payload;
  timingJson.value = JSON.stringify(payload, null, 2);
  applyTimingArray(result, { source: "demo" });
  logClient("info", "demo_timing_generated", "Se generaron timings DEMO; no corresponden a IA real.");
}

function renderTable() {
  if (!state.words.length) {
    timingsBody.innerHTML = '<tr><td colspan="7" class="empty">Pega una letra maestra para preparar las palabras.</td></tr>';
    return;
  }

  timingsBody.innerHTML = state.words.map((w, i) => {
    const dur = w.start !== null && w.end !== null ? w.end - w.start : null;
    const qa = w.qaStatus || null;
    const qaClass = qa ? ` qa-${qa}` : "";
    const qaTitle = w.qaScore != null ? ` title="Score QA: ${w.qaScore}/100"` : "";
    return `<tr data-row="${i}">
      <td>${i + 1}</td>
      <td>${w.lineIndex + 1}</td>
      <td><span class="qa-pill${qaClass}"${qaTitle}>${qaLabel(qa)}</span></td>
      <td class="master-word">${escapeHtml(w.text)}</td>
      <td>${fmtSec(w.start)}</td>
      <td>${fmtSec(w.end)}</td>
      <td>${fmtSec(dur)}</td>
    </tr>`;
  }).join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[c]));
}

function currentWordIndex(t) {
  let active = -1;
  let bestStarted = -1;
  let bestStart = -Infinity;

  for (let i = 0; i < state.words.length; i++) {
    const w = state.words[i];
    if (w.start === null || w.end === null) continue;

    if (t >= w.start && t <= w.end) {
      if (w.start >= bestStart) {
        bestStart = w.start;
        active = i;
      }
    }
    if (t >= w.start && w.start >= bestStart) {
      bestStart = w.start;
      bestStarted = i;
    }
  }
  return active >= 0 ? active : bestStarted;
}

function currentPage(t) {
  if (!state.lines.length) return 0;
  const wi = currentWordIndex(t);
  const lineIndex = wi >= 0 ? state.words[wi].lineIndex : 0;
  return Math.floor(lineIndex / state.linesPerPage);
}

function fitFontForLine(line) {
  let size = state.fontSize;
  const maxWidth = 276;
  while (size > 12) {
    pvx.font = `700 ${size}px Impact, Arial Black, sans-serif`;
    const width = line.words.reduce((sum, w, i) => {
      const ww = pvx.measureText(w.text).width;
      const sp = i ? pvx.measureText(" ").width : 0;
      return sum + sp + ww;
    }, 0);
    if (width <= maxWidth) break;
    size -= 1;
  }
  return size;
}

function drawWord(text, x, baseline, size, frac) {
  pvx.font = `700 ${size}px Impact, Arial Black, sans-serif`;
  pvx.textBaseline = "alphabetic";
  pvx.lineJoin = "round";
  pvx.lineWidth = 3;
  pvx.strokeStyle = "#000";
  pvx.fillStyle = "#fff";
  pvx.strokeText(text, x, baseline);
  pvx.fillText(text, x, baseline);

  if (frac > 0) {
    const width = pvx.measureText(text).width;
    pvx.save();
    pvx.beginPath();
    pvx.rect(x - 2, baseline - size - 5, width * clamp(frac, 0, 1) + 4, size + 12);
    pvx.clip();
    pvx.strokeStyle = "#000";
    pvx.fillStyle = "#8B5CF6";
    pvx.strokeText(text, x, baseline);
    pvx.fillText(text, x, baseline);
    pvx.restore();
  }
}

function drawPreview() {
  const t = Number(player.currentTime) || 0;
  pvx.clearRect(0, 0, pv.width, pv.height);
  pvx.fillStyle = "#000";
  pvx.fillRect(0, 0, pv.width, pv.height);

  if (!state.lines.length) {
    pvx.fillStyle = "#777";
    pvx.font = "14px Arial, sans-serif";
    pvx.textAlign = "center";
    pvx.fillText("PEGA LA LETRA MAESTRA", 150, 108);
    pvx.textAlign = "left";
    pvInfo.textContent = "—";
    return;
  }

  const page = currentPage(t);
  const startLine = page * state.linesPerPage;
  const pageLines = state.lines.slice(startLine, startLine + state.linesPerPage);
  const lineHeight = 30;
  const contentHeight = pageLines.length * lineHeight;
  const top = Math.max(18, Math.round((216 - contentHeight) / 2));

  pageLines.forEach((line, localLine) => {
    const size = fitFontForLine(line);
    pvx.font = `700 ${size}px Impact, Arial Black, sans-serif`;
    const space = pvx.measureText(" ").width;
    const widths = line.words.map(w => pvx.measureText(w.text).width);
    const total = widths.reduce((a, b) => a + b, 0) + space * Math.max(0, line.words.length - 1);
    let x = Math.max(12, (300 - total) / 2);
    const baseline = top + localLine * lineHeight + size;

    line.words.forEach((w, wi) => {
      let frac = 0;
      if (w.start !== null && w.end !== null) {
        if (t >= w.end) frac = 1;
        else if (t > w.start) frac = (t - w.start) / Math.max(0.03, w.end - w.start);
      }
      drawWord(w.text, x, baseline, size, frac);
      x += widths[wi] + space;
    });
  });

  const wi = currentWordIndex(t);
  state.activeWord = wi;
  const active = wi >= 0 ? state.words[wi] : null;
  pvInfo.textContent = `Pantalla ${page + 1}/${Math.max(1, Math.ceil(state.lines.length / state.linesPerPage))}${active ? " · " + active.text : ""}`;

  document.querySelectorAll("#timingsBody tr.active").forEach(el => el.classList.remove("active"));
  if (wi >= 0) {
    const row = document.querySelector(`#timingsBody tr[data-row="${wi}"]`);
    if (row) row.classList.add("active");
  }
}

function updateTransport() {
  const duration = Number(player.duration);
  const current = Number(player.currentTime) || 0;
  timeNow.textContent = fmtTime(current);
  timeTotal.textContent = Number.isFinite(duration) ? fmtTime(duration) : "00:00.000";
  if (Number.isFinite(duration) && duration > 0) {
    seek.max = String(duration);
    if (!seek.matches(":active")) seek.value = String(current);
  }
  playBtn.textContent = player.paused ? "▶ Play" : "❚❚ Pausa";
}

function animationLoop() {
  updateTransport();
  drawPreview();
  requestAnimationFrame(animationLoop);
}

audioInput.addEventListener("change", () => {
  clearError();
  const file = audioInput.files[0];
  if (!file) return;
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = URL.createObjectURL(file);
  player.src = state.objectUrl;
  player.load();
});

player.addEventListener("loadedmetadata", () => {
  seek.max = String(player.duration || 1);
  timeTotal.textContent = fmtTime(player.duration);
});

masterLyrics.addEventListener("input", () => {
  clearTimings();
  parseMasterLyrics();
});


baseSelectBtn.addEventListener("click", () => selectEngine("base"));
scribeSelectBtn.addEventListener("click", () => selectEngine("scribe"));

runSelectedBtn.addEventListener("click", () => {
  clearError();
  if (state.selectedEngine === "scribe") {
    if (scribeBtn.disabled) return showError("ElevenLabs Scribe v2 no está disponible en este momento.");
    runSelectedBtn.disabled = true;
    scribeBtn.click();
  } else {
    if (syncBtn.disabled) return showError("BASE v2 local no está disponible en este momento.");
    runSelectedBtn.disabled = true;
    syncBtn.click();
  }
});

syncBtn.addEventListener("click", async () => {
  clearError();
  state.lastRawResult = null;
  state.lastApplyWarnings = [];
  timingJson.value = "";
  setTimingSource("none");
  parseMasterLyrics();

  if (!audioInput.files.length) {
    return showError("Selecciona primero la pista de voces/acapella.");
  }
  if (!state.words.length) {
    return showError("Pega primero la letra maestra exacta.");
  }

  syncBtn.disabled = true;
  demoBtn.disabled = true;
  aiProgress.classList.remove("hidden");
  aiProgressBar.style.width = "12%";
  aiStatus.textContent = "Subiendo audio al OVH…";
  aiMeta.textContent = audioInput.files[0].name;

  let pct = 18;
  let phase = 0;
  const phases = [
    "faster-whisper está localizando la voz…",
    "Comparando la voz con la letra maestra…",
    "Alineador español ajustando palabra por palabra…",
    "Preparando timings para el preview…",
  ];
  const started = performance.now();
  const ticker = setInterval(() => {
    pct = Math.min(88, pct + 3);
    aiProgressBar.style.width = pct + "%";
    phase = Math.min(phases.length - 1, Math.floor((pct - 18) / 20));
    aiStatus.textContent = phases[phase];
    aiMeta.textContent = `${((performance.now() - started) / 1000).toFixed(0)} s · procesamiento local CPU`;
  }, 1800);

  try {
    const form = new FormData();
    form.append("audio", audioInput.files[0]);
    form.append("lyrics", masterLyrics.value);

    const requestId = "lab-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    logClient("info", "ia_request_started", "Enviando audio + letra al OVH.", {
      request_id: requestId,
      filename: audioInput.files[0].name,
      bytes: audioInput.files[0].size,
      master_words: state.words.length,
    });

    const result = await apiJson("api/local/align", {
      method: "POST",
      body: form,
      headers: { "X-Debug-Request-ID": requestId },
    });

    clearInterval(ticker);
    aiProgressBar.style.width = "100%";
    aiStatus.textContent = "Sincronización IA completada ✓";

    const metrics = result.metrics || {};
    const anchors = result.rough_asr || {};
    aiMeta.textContent =
      `${metrics.aligned_words ?? 0} alineadas · ${metrics.interpolated_words ?? 0} interpoladas · ${result.elapsed_s ?? "—"} s · anclajes ${Math.round((anchors.master_anchor_ratio || 0) * 100)}%`;

    state.lastRawResult = result;
    state.lastAiResult = result;
    renderJobMetrics(result);
    restoreAiBtn.classList.remove("hidden");
    timingJson.value = JSON.stringify(result, null, 2);
    jsonPanel.classList.remove("hidden");
    logClient("info", "ia_result_received", "El navegador recibió el JSON real de la IA.", {
      request_id: requestId,
      words: Array.isArray(result.words) ? result.words.length : null,
      metrics,
    });

    const applied = applyTimingArray(result.words || [], { source: "ai" });

    previewStatus.textContent = applied.warnings.length
      ? `IA REAL · ${applied.warnings.length} warning(s)`
      : (metrics.interpolated_words ? `IA REAL · ${metrics.interpolated_words} revisar` : "IA REAL lista ✓");
    previewStatus.classList.add("ok");
  } catch (e) {
    clearInterval(ticker);
    aiProgressBar.style.width = "100%";
    aiStatus.textContent = state.lastRawResult ? "La IA respondió, pero falló la aplicación" : "La sincronización falló";
    aiMeta.textContent = "";
    if (state.lastRawResult) {
      setTimingSource("ai-error");
      jsonPanel.classList.remove("hidden");
    }
    logClient("error", "ia_apply_or_request_error", e.message || String(e), {
      raw_result_received: Boolean(state.lastRawResult),
    });
    showError(e.message || String(e));
  } finally {
    demoBtn.disabled = false;
    runSelectedBtn.disabled = false;
    await checkLocalEngine();
    await checkElevenLabsEngine();
  }
});


scribeBtn.addEventListener("click", async () => {
  clearError();
  state.lastRawResult = null;
  state.lastApplyWarnings = [];
  timingJson.value = "";
  setTimingSource("none");
  parseMasterLyrics();

  if (!audioInput.files.length) {
    return showError("Selecciona primero la pista de voces/acapella.");
  }
  syncBtn.disabled = true;
  scribeBtn.disabled = true;
  const originalScribeLabel = scribeBtn.textContent;
  scribeBtn.textContent = "⏳ Scribe v2 procesando…";
  demoBtn.disabled = true;
  aiProgress.classList.remove("hidden");
  aiProgressBar.style.width = "15%";
  aiStatus.textContent = "Subiendo acapella a ElevenLabs Scribe v2…";
  aiMeta.textContent = audioInput.files[0].name;

  let pct = 22;
  const started = performance.now();
  const ticker = setInterval(() => {
    pct = Math.min(90, pct + 5);
    aiProgressBar.style.width = pct + "%";
    aiStatus.textContent = pct < 58
      ? "Scribe v2 está transcribiendo con timestamps por palabra…"
      : "Comparando Scribe contra la letra maestra…";
    aiMeta.textContent = `${((performance.now() - started) / 1000).toFixed(0)} s · ElevenLabs API`;
  }, 900);

  try {
    const form = new FormData();
    form.append("audio", audioInput.files[0]);
    form.append("lyrics", masterLyrics.value || "");
    form.append("language_code", "spa");

    const requestId = "scribe-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    logClient("info", "scribe_request_started", "Enviando audio a ElevenLabs Scribe v2.", {
      request_id: requestId,
      filename: audioInput.files[0].name,
      bytes: audioInput.files[0].size,
      master_words: state.words.length,
    });

    const result = await apiJson("api/elevenlabs/transcribe", {
      method: "POST",
      body: form,
      headers: { "X-Debug-Request-ID": requestId },
    });

    clearInterval(ticker);
    aiProgressBar.style.width = "100%";
    aiStatus.textContent = "ElevenLabs Scribe v2 completado ✓";

    const metrics = result.metrics || {};
    const coverage = Math.round(Number(metrics.coverage_ratio || 0) * 100);

    if (!state.words.length && result.source_mode === "scribe_only") {
      masterLyrics.value = buildLyricsFromScribeWords(result.words || []);
      parseMasterLyrics();
      logClient("info", "scribe_lyrics_loaded", "La transcripción de Scribe se cargó como letra de trabajo para el preview.", {
        words: state.words.length,
        lines: state.lines.length,
      });
    }

    aiMeta.textContent =
      `${metrics.scribe_word_count ?? 0} Scribe · ${metrics.mapped_words ?? 0}/${metrics.master_word_count ?? 0} mapeadas · ${coverage}% · ${result.elapsed_s ?? "—"} s`;

    state.lastRawResult = result;
    state.lastAiResult = result;
    state.lastEngineSource = "scribe";
    renderJobMetrics(result);
    restoreAiBtn.classList.remove("hidden");
    timingJson.value = JSON.stringify(result, null, 2);
    jsonPanel.classList.remove("hidden");

    if (scribePanel) scribePanel.classList.remove("hidden");
    if (scribeTranscript) scribeTranscript.value = result?.scribe?.text || "";
    if (scribeCompareMeta) {
      scribeCompareMeta.textContent = result.source_mode === "scribe_only"
        ? `Modo transcripción directa · ${metrics.scribe_word_count ?? 0} palabras con timestamps`
        : `Exactas: ${metrics.exact_matches ?? 0} · fuzzy: ${metrics.fuzzy_matches ?? 0} · agrupadas: ${metrics.grouped_matches ?? 0} · interpoladas: ${metrics.interpolated_words ?? 0}`;
    }
    if (scribeCompareBadge) {
      scribeCompareBadge.classList.remove("ok", "warn", "bad");
      if (coverage >= 97) {
        scribeCompareBadge.textContent = `🟢 ${coverage}% mapeado`;
        scribeCompareBadge.classList.add("ok");
      } else if (coverage >= 90) {
        scribeCompareBadge.textContent = `🟡 ${coverage}% mapeado`;
        scribeCompareBadge.classList.add("warn");
      } else {
        scribeCompareBadge.textContent = `🔴 ${coverage}% mapeado`;
        scribeCompareBadge.classList.add("bad");
      }
    }

    logClient("info", "scribe_result_received", "Se recibió Scribe v2 y el matching contra la letra maestra.", {
      request_id: requestId,
      metrics,
    });

    const applied = applyTimingArray(result.words || [], { source: "scribe" });
    previewStatus.textContent = applied.warnings.length
      ? `SCRIBE V2 · ${applied.warnings.length} warning(s)`
      : `SCRIBE V2 · ${coverage}% mapeado`;
    previewStatus.classList.add("ok");
  } catch (e) {
    clearInterval(ticker);
    aiProgressBar.style.width = "100%";
    aiStatus.textContent = "ElevenLabs Scribe v2 falló";
    aiMeta.textContent = "";
    logClient("error", "scribe_request_error", e.message || String(e));
    showError(e.message || String(e));
  } finally {
    if (typeof originalScribeLabel !== "undefined") scribeBtn.textContent = originalScribeLabel;
    demoBtn.disabled = false;
    runSelectedBtn.disabled = false;
    await checkLocalEngine();
    await checkElevenLabsEngine();
  }
});

demoBtn.addEventListener("click", generateDemoTimings);

restoreAiBtn.addEventListener("click", () => {
  clearError();
  if (!state.lastAiResult || !Array.isArray(state.lastAiResult.words)) {
    return showError("No hay un resultado IA real guardado en esta sesión.");
  }
  state.lastRawResult = state.lastAiResult;
  renderJobMetrics(state.lastAiResult);
  timingJson.value = JSON.stringify(state.lastAiResult, null, 2);
  jsonPanel.classList.remove("hidden");
  const restoreSource = state.lastAiResult.engine === "elevenlabs-scribe-v2" ? "scribe" : "ai";
  const applied = applyTimingArray(state.lastAiResult.words, { source: restoreSource });
  const metrics = state.lastAiResult.metrics || {};
  previewStatus.textContent = restoreSource === "scribe"
    ? (applied.warnings.length ? `SCRIBE V2 · ${applied.warnings.length} warning(s)` : `SCRIBE V2 · ${Math.round(Number(metrics.coverage_ratio || 0) * 100)}% mapeado`)
    : (applied.warnings.length
      ? `IA REAL · ${applied.warnings.length} warning(s)`
      : (metrics.interpolated_words ? `IA REAL · ${metrics.interpolated_words} revisar` : "IA REAL lista ✓"));
  previewStatus.classList.add("ok");
  if (state.lastAiResult.engine === "elevenlabs-scribe-v2") {
    if (scribePanel) scribePanel.classList.remove("hidden");
    if (scribeTranscript) scribeTranscript.value = state.lastAiResult?.scribe?.text || "";
  }
  logClient("info", "ia_result_restored", "Se restauró el último resultado IA REAL sin reprocesar el audio.");
});

toggleJsonBtn.addEventListener("click", () => {
  jsonPanel.classList.toggle("hidden");
});

applyJsonBtn.addEventListener("click", () => {
  clearError();
  try {
    const payload = JSON.parse(timingJson.value);
    state.lastRawResult = payload;
    const source = payload.engine === "demo" ? "demo" : "json";
    applyTimingArray(payload.words, { source });
  } catch (e) {
    logClient("error", "manual_json_apply_error", e.message || String(e));
    showError(e.message || String(e));
  }
});

clearBtn.addEventListener("click", clearTimings);

playBtn.addEventListener("click", async () => {
  clearError();
  if (!player.src) return showError("Selecciona primero un audio.");
  if (player.paused) {
    try {
      await player.play();
    } catch (e) {
      showError("No se pudo iniciar el audio: " + (e.message || e));
    }
  } else {
    player.pause();
  }
});

backBtn.addEventListener("click", () => {
  player.currentTime = clamp((player.currentTime || 0) - 5, 0, player.duration || 0);
});

fwdBtn.addEventListener("click", () => {
  player.currentTime = clamp((player.currentTime || 0) + 5, 0, player.duration || 0);
});

seek.addEventListener("input", () => {
  const v = Number(seek.value);
  if (Number.isFinite(v)) player.currentTime = v;
});

refreshLogBtn.addEventListener("click", renderDiagnosticLog);
copyLogBtn.addEventListener("click", copyDiagnosticLog);

window.addEventListener("beforeunload", () => {
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
});

parseMasterLyrics();
setTimingSource("none");
updateEngineSelector();
checkLocalEngine();
checkElevenLabsEngine();
renderDiagnosticLog();
requestAnimationFrame(animationLoop);
