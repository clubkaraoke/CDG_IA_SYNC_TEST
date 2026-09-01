const $ = (id) => document.getElementById(id);
const audio = $("audio");
const startBtn = $("startBtn");
const progressCard = $("progressCard");
const resultCard = $("resultCard");
const errorCard = $("errorCard");
const errorText = $("errorText");
const statusTitle = $("statusTitle");
const statusMeta = $("statusMeta");
const progressBar = $("progressBar");
const wordsBody = $("wordsBody");
const plainText = $("plainText");
const rawJson = $("rawJson");
const resultSummary = $("resultSummary");
const copyBtn = $("copyBtn");
const configCard = $("configCard");
const tokenInput = $("tokenInput");
const saveTokenBtn = $("saveTokenBtn");
let lastText = "";

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

async function health() {
  try {
    const h = await apiJson("api/health");
    const badge = $("apiBadge");
    badge.textContent = h.mvsep_configured ? "MVSEP configurado ✓" : "Falta API token";
    badge.classList.toggle("ok", h.mvsep_configured);
    configCard.classList.toggle("hidden", h.mvsep_configured);
    startBtn.disabled = !h.mvsep_configured;
    return h;
  } catch {
    $("apiBadge").textContent = "Backend no disponible";
    startBtn.disabled = true;
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
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    saveTokenBtn.disabled = false;
  }
});

function setStatus(status, data) {
  const labels = {
    waiting: "En cola de MVSEP…",
    processing: "Parakeet está transcribiendo…",
    distributing: "Distribuyendo trabajo…",
    merging: "Uniendo resultados…",
    done: "IA completada ✓",
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

function fmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(3) + " s";
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
    wordsBody.innerHTML = `<tr><td colspan="5" class="empty">Todavía no reconocemos automáticamente el formato de timestamps. Mira “Letra” y “MVSEP crudo”; esa primera prueba nos dirá cómo adaptar el parser.</td></tr>`;
  }
  plainText.textContent = rawText || "No se encontró salida textual automáticamente.";
  rawJson.textContent = JSON.stringify(r, null, 2);
  lastText = rawText;
  resultSummary.textContent = words.length
    ? `${words.length} elementos temporizados · formato ${parsed.format}`
    : "Resultado recibido · necesitamos identificar el formato exacto";
  resultCard.classList.remove("hidden");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

startBtn.addEventListener("click", async () => {
  clearError();
  resultCard.classList.add("hidden");
  if (!audio.files.length) return showError("Selecciona primero una pista de voces.");
  startBtn.disabled = true;
  progressCard.classList.remove("hidden");
  progressBar.style.width = "8%";
  statusTitle.textContent = "Subiendo audio a MVSEP…";
  statusMeta.textContent = audio.files[0].name;

  try {
    const form = new FormData();
    form.append("audio", audio.files[0]);
    const created = await apiJson("api/transcribe", { method: "POST", body: form });
    const hash = created.hash;
    statusMeta.textContent = `Trabajo: ${hash}`;

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
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    const h = await health();
    startBtn.disabled = !h?.mvsep_configured;
  }
});

copyBtn.addEventListener("click", async () => {
  if (!lastText) return;
  await navigator.clipboard.writeText(lastText);
  copyBtn.textContent = "Copiado ✓";
  setTimeout(() => copyBtn.textContent = "Copiar texto", 1200);
});

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.target).classList.add("active");
  });
});

health();
