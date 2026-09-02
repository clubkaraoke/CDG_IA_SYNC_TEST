const $ = (id) => document.getElementById(id);

const audioInput = $("audio");
const masterLyrics = $("masterLyrics");
const lyricsMeta = $("lyricsMeta");
const syncBtn = $("syncBtn");
const demoBtn = $("demoBtn");
const engineBadge = $("engineBadge");
const aiProgress = $("aiProgress");
const aiProgressBar = $("aiProgressBar");
const aiStatus = $("aiStatus");
const aiMeta = $("aiMeta");
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
  lastApplyWarnings: [],
};

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v));
}

function showError(message) {
  errorText.textContent = message;
  errorCard.classList.remove("hidden");
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
  } catch (e) {
    engineBadge.textContent = "Motor IA no disponible";
    engineBadge.classList.remove("ok");
    syncBtn.disabled = true;
  }
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
  state.lastApplyWarnings = [];
  timingJson.value = "";
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
    timingsBody.innerHTML = '<tr><td colspan="6" class="empty">Pega una letra maestra para preparar las palabras.</td></tr>';
    return;
  }

  timingsBody.innerHTML = state.words.map((w, i) => {
    const dur = w.start !== null && w.end !== null ? w.end - w.start : null;
    return `<tr data-row="${i}">
      <td>${i + 1}</td>
      <td>${w.lineIndex + 1}</td>
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
    await checkLocalEngine();
  }
});

demoBtn.addEventListener("click", generateDemoTimings);

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
checkLocalEngine();
renderDiagnosticLog();
requestAnimationFrame(animationLoop);
