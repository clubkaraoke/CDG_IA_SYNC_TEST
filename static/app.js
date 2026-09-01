const $ = (id) => document.getElementById(id);
const audio = $("audio");
const startBtn = $("startBtn");
const qwenBtn = $("qwenBtn");
const progressCard = $("progressCard");
const resultCard = $("resultCard");
const qwenResultCard = $("qwenResultCard");
const errorCard = $("errorCard");
const errorText = $("errorText");
const statusTitle = $("statusTitle");
const statusMeta = $("statusMeta");
const progressBar = $("progressBar");
const wordsBody = $("wordsBody");
const qwenWordsBody = $("qwenWordsBody");
const plainText = $("plainText");
const rawJson = $("rawJson");
const qwenRaw = $("qwenRaw");
const resultSummary = $("resultSummary");
const qwenSummary = $("qwenSummary");
const qwenMeta = $("qwenMeta");
const copyBtn = $("copyBtn");
const copyQwenBtn = $("copyQwenBtn");
const configCard = $("configCard");
const tokenInput = $("tokenInput");
const saveTokenBtn = $("saveTokenBtn");
const qwenConfigCard = $("qwenConfigCard");
const qwenEndpointInput = $("qwenEndpointInput");
const qwenTokenInput = $("qwenTokenInput");
const saveQwenBtn = $("saveQwenBtn");
const inspectLyricsBtn = $("inspectLyricsBtn");
const masterLyrics = $("masterLyrics");
const lyricsBadge = $("lyricsBadge");
const logsList = $("logsList");
const refreshLogsBtn = $("refreshLogsBtn");
const planBadge = $("planBadge");

let lastText = "";
let lastQwen = "";

function showError(message) {
  errorText.textContent = message;
  errorCard.classList.remove("hidden");
}
function clearError() { errorCard.classList.add("hidden"); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function apiJson(url, options) {
  const r = await fetch(url, options);
  let data;
  try { data = await r.json(); } catch { data = { detail: `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.detail || data.message || `HTTP ${r.status}`);
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}
function fmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(3) + " s";
}

async function health() {
  try {
    const h = await apiJson("api/health");
    const apiBadge = $("apiBadge");
    apiBadge.textContent = h.mvsep_configured ? "MVSEP configurado ✓" : "Falta MVSEP";
    apiBadge.classList.toggle("ok", h.mvsep_configured);
    configCard.classList.toggle("hidden", h.mvsep_configured);
    startBtn.disabled = !h.mvsep_configured;

    const qBadge = $("qwenBadge");
    qBadge.textContent = h.qwen_configured ? "Qwen GPU configurado ✓" : "Qwen GPU pendiente";
    qBadge.classList.toggle("ok", h.qwen_configured);
    qwenConfigCard.classList.toggle("hidden", h.qwen_configured);
    qwenBtn.disabled = !h.qwen_configured;
    return h;
  } catch {
    $("apiBadge").textContent = "Backend no disponible";
    startBtn.disabled = true;
    qwenBtn.disabled = true;
  }
}

saveTokenBtn.addEventListener("click", async () => {
  clearError();
  const token = tokenInput.value.trim();
  if (!token) return showError("Pega primero tu API token de MVSEP.");
  saveTokenBtn.disabled = true;
  try {
    await apiJson("api/config/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token })
    });
    tokenInput.value = "";
    await health();
  } catch (e) { showError(e.message || String(e)); }
  finally { saveTokenBtn.disabled = false; }
});

saveQwenBtn.addEventListener("click", async () => {
  clearError();
  const endpoint = qwenEndpointInput.value.trim();
  if (!endpoint) return showError("Falta el endpoint del Qwen GPU Worker.");
  saveQwenBtn.disabled = true;
  try {
    await apiJson("api/qwen/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint, token: qwenTokenInput.value.trim() })
    });
    qwenTokenInput.value = "";
    await health();
    try {
      const h = await apiJson("api/qwen/health");
      $("qwenBadge").textContent = `Qwen GPU ✓ · ${h.worker.device || "CUDA"}`;
    } catch (e) {
      showError("Endpoint guardado, pero el worker todavía no responde: " + e.message);
    }
  } catch (e) { showError(e.message || String(e)); }
  finally { saveQwenBtn.disabled = false; }
});

inspectLyricsBtn.addEventListener("click", async () => {
  clearError();
  if (!audio.files.length) return showError("Selecciona primero una pista de voces.");
  inspectLyricsBtn.disabled = true;
  lyricsBadge.textContent = "Buscando letra en metadatos…";
  try {
    const form = new FormData();
    form.append("audio", audio.files[0]);
    const r = await apiJson("api/inspect-audio", { method: "POST", body: form });
    if (r.found) {
      masterLyrics.value = r.lyrics || "";
      lyricsBadge.textContent = `✓ Letra embebida encontrada (${r.source})`;
      lyricsBadge.classList.add("good-text");
    } else {
      lyricsBadge.textContent = "No hay letra embebida · Qwen ASR deberá transcribir";
      lyricsBadge.classList.remove("good-text");
    }
    await refreshDiagnostics();
  } catch (e) {
    lyricsBadge.textContent = "No se pudo leer la letra embebida";
    showError(e.message || String(e));
  } finally { inspectLyricsBtn.disabled = false; }
});

function setStatus(status, data) {
  const labels = {
    waiting: "En cola de MVSEP…",
    processing: "Parakeet está transcribiendo…",
    distributing: "Distribuyendo trabajo…",
    merging: "Uniendo resultados…",
    done: "Parakeet completado ✓",
    failed: "MVSEP reportó un fallo",
  };
  statusTitle.textContent = labels[status] || `Estado: ${status}`;
  const meta = data?.data || {};
  if (status === "waiting") {
    statusMeta.textContent = `Orden actual: ${meta.current_order ?? "—"} · Cola: ${meta.queue_count ?? "—"}`;
    progressBar.style.width = "22%";
  } else if (status === "processing") {
    statusMeta.textContent = meta.message || "Procesando audio";
    progressBar.style.width = "62%";
  } else if (["distributing", "merging"].includes(status)) {
    statusMeta.textContent = meta.message || "Procesando";
    progressBar.style.width = "80%";
  } else if (status === "done") {
    progressBar.style.width = "100%";
    statusMeta.textContent = meta.algorithm_description || meta.algorithm || "Parakeet v3";
  }
}

function renderResult(r) {
  const parsed = r.best_parse || {};
  const words = parsed.words || [];
  const rawText = parsed.raw_text || "";
  wordsBody.innerHTML = "";
  words.forEach((w, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${i + 1}</td><td>${fmt(w.start)}</td><td>${fmt(w.end)}</td><td>${escapeHtml(w.word || "")}</td><td>${escapeHtml(w.source || "")}</td>`;
    wordsBody.appendChild(tr);
  });
  if (!words.length) {
    wordsBody.innerHTML = '<tr><td colspan="5" class="empty">Sin timings reconocidos.</td></tr>';
  }
  plainText.textContent = rawText || "No se encontró salida textual automáticamente.";
  rawJson.textContent = JSON.stringify(r, null, 2);
  lastText = rawText;
  const unit = parsed.format === "subtitle_segments" ? "segmentos temporizados" : "elementos temporizados";
  resultSummary.textContent = words.length ? `${words.length} ${unit}` : "Resultado recibido";
  resultCard.classList.remove("hidden");
}

function renderQwen(r) {
  const q = r.qwen || {};
  const words = q.words || [];
  qwenWordsBody.innerHTML = "";
  words.forEach((w, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${i + 1}</td><td>${fmt(w.start)}</td><td>${fmt(w.end)}</td><td>${escapeHtml(w.word || "")}</td>`;
    qwenWordsBody.appendChild(tr);
  });
  if (!words.length) {
    qwenWordsBody.innerHTML = '<tr><td colspan="4" class="empty">Qwen no devolvió palabras temporizadas.</td></tr>';
  }
  qwenSummary.textContent = `${words.length} palabras temporizadas`;
  qwenMeta.textContent = `Modo: ${q.mode || "—"} · Letra: ${r.lyrics_source || "—"} · ${q.elapsed_s ?? "—"} s`;
  qwenRaw.textContent = JSON.stringify(r, null, 2);
  lastQwen = qwenRaw.textContent;
  qwenResultCard.classList.remove("hidden");
}

startBtn.addEventListener("click", async () => {
  clearError();
  if (!audio.files.length) return showError("Selecciona primero una pista de voces.");
  startBtn.disabled = true;
  progressCard.classList.remove("hidden");
  progressBar.style.width = "8%";
  statusTitle.textContent = "Subiendo audio a MVSEP…";
  statusMeta.textContent = audio.files[0].name;

  try {
    const form = new FormData();
    form.append("audio", audio.files[0]);
    const debugId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
    const created = await apiJson("api/transcribe", {
      method: "POST", body: form, headers: { "X-Debug-Request-Id": debugId }
    });
    const hash = created.hash;
    let statusData;
    for (;;) {
      statusData = await apiJson(`api/status/${encodeURIComponent(hash)}`);
      setStatus(statusData.status, statusData);
      if (statusData.status === "done") break;
      if (["failed", "not_found", "error"].includes(statusData.status)) {
        throw new Error(statusData?.data?.message || `Estado final: ${statusData.status}`);
      }
      await sleep(5000);
    }
    const result = await apiJson(`api/result/${encodeURIComponent(hash)}`);
    renderResult(result);
  } catch (e) { showError(e.message || String(e)); }
  finally {
    const h = await health();
    startBtn.disabled = !h?.mvsep_configured;
    await refreshDiagnostics();
  }
});

qwenBtn.addEventListener("click", async () => {
  clearError();
  if (!audio.files.length) return showError("Selecciona primero una pista de voces.");
  qwenBtn.disabled = true;
  progressCard.classList.remove("hidden");
  progressBar.style.width = "35%";
  statusTitle.textContent = "Enviando a Qwen GPU…";
  statusMeta.textContent = masterLyrics.value.trim()
    ? "Usando letra maestra → Forced Aligner"
    : "Sin letra maestra → Qwen3-ASR 1.7B → Forced Aligner";

  try {
    const form = new FormData();
    form.append("audio", audio.files[0]);
    form.append("lyrics", masterLyrics.value.trim());
    form.append("prefer_embedded", "true");
    form.append("language", "Spanish");
    const r = await apiJson("api/qwen/process", { method: "POST", body: form });
    progressBar.style.width = "100%";
    statusTitle.textContent = "Qwen completado ✓";
    statusMeta.textContent = `${r.qwen.word_count || 0} palabras · ${r.qwen.elapsed_s || "—"} s`;
    renderQwen(r);
  } catch (e) {
    showError(e.message || String(e));
    statusTitle.textContent = "Qwen falló";
  } finally {
    const h = await health();
    qwenBtn.disabled = !h?.qwen_configured;
    await refreshDiagnostics();
  }
});

copyBtn.addEventListener("click", async () => {
  if (!lastText) return;
  await navigator.clipboard.writeText(lastText);
  copyBtn.textContent = "Copiado ✓";
  setTimeout(() => copyBtn.textContent = "Copiar texto", 1200);
});
copyQwenBtn.addEventListener("click", async () => {
  if (!lastQwen) return;
  await navigator.clipboard.writeText(lastQwen);
  copyQwenBtn.textContent = "Copiado ✓";
  setTimeout(() => copyQwenBtn.textContent = "Copiar JSON", 1200);
});

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.target).classList.add("active");
  });
});

function localTime(iso) {
  try { return new Date(iso).toLocaleTimeString("es-PE", { hour12: false }); }
  catch { return iso || ""; }
}
function renderLogs(events) {
  if (!events?.length) {
    logsList.innerHTML = '<div class="empty">Sin eventos todavía.</div>';
    return;
  }
  logsList.innerHTML = events.map(e => {
    const d = e.details || {};
    const extras = [];
    if (e.filename) extras.push(e.filename);
    if (e.job_hash) extras.push("hash " + e.job_hash.slice(0, 10) + "…");
    if (d.current_order != null) extras.push("posición " + d.current_order);
    if (d.queue_count != null) extras.push("cola " + d.queue_count);
    if (d.word_count != null) extras.push(d.word_count + " palabras");
    if (d.mode) extras.push(d.mode);
    if (d.elapsed_s != null) extras.push(d.elapsed_s + " s");
    return `<div class="log-row ${e.level === "error" ? "log-error" : ""}">
      <span class="log-time">${escapeHtml(localTime(e.ts))}</span>
      <span class="log-event">${escapeHtml(e.event || "")}</span>
      <span class="log-message">${escapeHtml(e.message || "")}${extras.length ? " · " + escapeHtml(extras.join(" · ")) : ""}</span>
    </div>`;
  }).join("");
}
async function refreshDiagnostics() {
  try {
    const [logs, queue] = await Promise.all([
      apiJson("api/logs?limit=200"),
      apiJson("api/queue").catch(() => null)
    ]);
    renderLogs(logs.events || []);
    const p = queue?.mvsep?.plan;
    if (p) {
      const planName = p.plan ?? p.name ?? "—";
      const q = p.queue ?? "—";
      planBadge.textContent = `Plan: ${planName} · Cola: ${q}`;
      planBadge.classList.add("ok");
    } else {
      planBadge.textContent = "Plan MVSEP: no disponible";
      planBadge.classList.remove("ok");
    }
  } catch (e) { console.warn("diagnostics", e); }
}

refreshLogsBtn.addEventListener("click", refreshDiagnostics);
setInterval(refreshDiagnostics, 5000);
health();
refreshDiagnostics();
