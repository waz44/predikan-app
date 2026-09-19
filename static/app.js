// ---------------------------------------------------------------------------
// Globalt state
// ---------------------------------------------------------------------------
let wavesurfer = null;
let regionsPlugin = null;
let activeRegion = null;
let currentFileId = null;
let audioDuration = 0;
let latestStats = null;

const uploadStatus = document.getElementById("uploadStatus");
const stepTrim = document.getElementById("step-trim");
const stepMetadata = document.getElementById("step-metadata");

// ---------------------------------------------------------------------------
// Prestandastatistik & tidsuppskattning
// ---------------------------------------------------------------------------
async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    latestStats = await res.json();
    renderStatsSummary(latestStats);
    updateEtaHint();
  } catch {
    // Statistik är en extra funktion - fel här ska aldrig blockera resten av appen.
  }
}

function renderStatsSummary(data) {
  const el = document.getElementById("statsSummary");
  if (!data || !data.total_count) {
    el.textContent = "Ingen bearbetning genomförd ännu.";
    return;
  }
  const ratioText = data.processing_ratio
    ? `${data.processing_ratio.toFixed(2)}x (bearbetningstid per sekund predikan)`
    : "-";
  el.innerHTML = `
    <p><strong>${data.total_count}</strong> predikningar bearbetade</p>
    <p>Total predikantid: <strong>${formatDuration(data.total_sermon_seconds)}</strong></p>
    <p>Total bearbetningstid: <strong>${formatDuration(data.total_processing_seconds)}</strong></p>
    <p>Snitthastighet: <strong>${ratioText}</strong></p>
  `;
}

function updateEtaHint() {
  const hint = document.getElementById("etaHint");
  if (!hint) return;
  if (!latestStats || !latestStats.processing_ratio) {
    hint.textContent = "";
    return;
  }
  const start = parseFloat(document.getElementById("startInput").value) || 0;
  const end = parseFloat(document.getElementById("endInput").value) || audioDuration;
  const clipSeconds = Math.max(0, end - start);
  const estimateSeconds = clipSeconds * latestStats.processing_ratio;
  hint.textContent = `⏱️ Uppskattad bearbetningstid för valt klipp: ~${formatDuration(estimateSeconds)} (baserat på snittet av ${latestStats.total_count} tidigare predikningar).`;
}

function formatDuration(totalSeconds) {
  const seconds = Math.round(totalSeconds || 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}min`;
  if (m > 0) return `${m}min ${s}s`;
  return `${s}s`;
}

loadStats();

// ---------------------------------------------------------------------------
// STEG 1: Uppladdning
// ---------------------------------------------------------------------------
document.getElementById("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;

  uploadStatus.textContent = "Laddar upp...";
  uploadStatus.className = "status";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Uppladdning misslyckades");
    }
    const data = await res.json();
    currentFileId = data.file_id;
    audioDuration = data.duration_seconds;

    uploadStatus.textContent = `✅ "${data.filename}" uppladdad (${formatTime(audioDuration)})`;
    uploadStatus.className = "status success";

    await initWaveform(currentFileId);
    stepTrim.classList.remove("hidden");
    stepMetadata.classList.remove("hidden");
  } catch (err) {
    uploadStatus.textContent = `❌ ${err.message}`;
    uploadStatus.className = "status error";
  }
});

// ---------------------------------------------------------------------------
// STEG 2: Vågform & klippning (Wavesurfer.js + Regions-plugin)
// ---------------------------------------------------------------------------
async function initWaveform(fileId) {
  if (wavesurfer) {
    wavesurfer.destroy();
  }

  regionsPlugin = WaveSurfer.Regions.create();

  wavesurfer = WaveSurfer.create({
    container: "#waveform",
    waveColor: "#c9c9ec",
    progressColor: "#4a3aff",
    cursorColor: "#23243a",
    height: 100,
    url: `/api/audio/${fileId}`,
    plugins: [regionsPlugin],
  });

  wavesurfer.on("ready", () => {
    const duration = wavesurfer.getDuration();
    // Skapa en förvald region som täcker hela klippet - användaren drar i kanterna
    activeRegion = regionsPlugin.addRegion({
      start: 0,
      end: duration,
      color: "rgba(74, 58, 255, 0.15)",
      drag: true,
      resize: true,
    });
    document.getElementById("startInput").value = 0;
    document.getElementById("endInput").value = duration.toFixed(1);
    updateEtaHint();
  });

  wavesurfer.on("audioprocess", () => {
    document.getElementById("currentTime").textContent = formatTime(wavesurfer.getCurrentTime());
  });

  wavesurfer.on("interaction", () => {
    document.getElementById("currentTime").textContent = formatTime(wavesurfer.getCurrentTime());
  });

  regionsPlugin.on("region-updated", (region) => {
    activeRegion = region;
    document.getElementById("startInput").value = region.start.toFixed(1);
    document.getElementById("endInput").value = region.end.toFixed(1);
    updateEtaHint();
  });
}

document.getElementById("playBtn").addEventListener("click", () => {
  if (wavesurfer) wavesurfer.play();
});

document.getElementById("stopBtn").addEventListener("click", () => {
  if (wavesurfer) wavesurfer.stop();
});

document.getElementById("backBtn").addEventListener("click", () => {
  if (wavesurfer) wavesurfer.skip(-15);
});

document.getElementById("forwardBtn").addEventListener("click", () => {
  if (wavesurfer) wavesurfer.skip(15);
});

document.getElementById("setStartBtn").addEventListener("click", () => {
  if (!wavesurfer) return;
  const t = wavesurfer.getCurrentTime();
  document.getElementById("startInput").value = t.toFixed(1);
  updateRegionFromInputs();
});

document.getElementById("setEndBtn").addEventListener("click", () => {
  if (!wavesurfer) return;
  const t = wavesurfer.getCurrentTime();
  document.getElementById("endInput").value = t.toFixed(1);
  updateRegionFromInputs();
});

document.getElementById("startInput").addEventListener("change", updateRegionFromInputs);
document.getElementById("endInput").addEventListener("change", updateRegionFromInputs);

function updateRegionFromInputs() {
  if (activeRegion) {
    const start = parseFloat(document.getElementById("startInput").value) || 0;
    const end = parseFloat(document.getElementById("endInput").value) || audioDuration;
    activeRegion.setOptions({ start, end });
  }
  updateEtaHint();
}

function formatTime(seconds) {
  const m = Math.floor(seconds / 60).toString().padStart(2, "0");
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

// ---------------------------------------------------------------------------
// STEG 3: Metadata -> lägg till i bearbetningskön
// Bearbetningen sker inte längre direkt här - formuläret lägger bara till
// objektet i den gemensamma kön (se sektionen "Bearbetningskö" nedan), och
// formuläret återställs direkt så nästa fil kan laddas upp och klippas utan
// att vänta på att den föregående hinner bearbetas klart.
// ---------------------------------------------------------------------------
document.getElementById("metadataForm").addEventListener("submit", async (e) => {
  e.preventDefault();

  if (!currentFileId) {
    alert("Ladda upp en ljudfil först.");
    return;
  }

  const payload = {
    file_id: currentFileId,
    start_seconds: parseFloat(document.getElementById("startInput").value) || 0,
    end_seconds: parseFloat(document.getElementById("endInput").value) || audioDuration,
    speaker: document.getElementById("speakerInput").value.trim(),
    title: document.getElementById("titleInput").value.trim(),
    description: document.getElementById("descriptionInput").value.trim(),
    category: document.getElementById("categoryInput").value.trim(),
    publish_date: document.getElementById("publishDateInput").value,
  };

  if (!payload.speaker) {
    alert("Talare är ett obligatoriskt fält.");
    return;
  }

  const addBtn = document.getElementById("processBtn");
  addBtn.disabled = true;

  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Kunde inte lägga till i kön");
    }

    uploadStatus.textContent = `✅ "${payload.title || payload.speaker}" tillagd i bearbetningskön.`;
    uploadStatus.className = "status success";

    // Återställ flödet så nästa fil kan laddas upp direkt
    currentFileId = null;
    audioDuration = 0;
    document.getElementById("fileInput").value = "";
    document.getElementById("metadataForm").reset();
    stepTrim.classList.add("hidden");
    stepMetadata.classList.add("hidden");
    if (wavesurfer) {
      wavesurfer.destroy();
      wavesurfer = null;
    }

    loadQueue();
  } catch (err) {
    alert(err.message);
  } finally {
    addBtn.disabled = false;
  }
});

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Bulk-import via CSV
// Raderna läggs till i samma bearbetningskö som manuellt klippta filer (se
// nedan) - status/framsteg för dem visas enbart i kösektionen.
// ---------------------------------------------------------------------------
document.getElementById("bulkImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("bulkCsvInput");
  const file = fileInput.files[0];
  const bulkStatus = document.getElementById("bulkImportStatus");

  if (!file) {
    bulkStatus.textContent = "Välj en CSV-fil först.";
    bulkStatus.className = "status error";
    return;
  }

  bulkStatus.textContent = "Läser in och validerar CSV-filen...";
  bulkStatus.className = "status";
  document.getElementById("bulkImportBtn").disabled = true;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/bulk-import", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Import misslyckades");
    }

    bulkStatus.textContent = `✅ ${data.items.length} predikning(ar) tillagda i bearbetningskön (se kön till höger).`;
    bulkStatus.className = "status success";
    fileInput.value = "";
    loadQueue();
  } catch (err) {
    bulkStatus.textContent = `❌ ${err.message}`;
    bulkStatus.className = "status error";
  } finally {
    document.getElementById("bulkImportBtn").disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Bearbetningskö: EN gemensam kö/vy för allt (manuellt klippta filer och
// CSV-bulkimport). Pollas regelbundet och visar för varje objekt dess
// status, procent och (när det pågår) samma stegvisa detaljvy som tidigare
// visades för ett enskilt jobb.
// ---------------------------------------------------------------------------
const QUEUE_STEP_ICONS = { pending: "⏳", running: "⚙️", done: "✅", skipped: "⏭️", error: "❌" };
const QUEUE_STATUS_LABELS = {
  queued: "⏳ I kö",
  running: "⚙️ Bearbetar...",
  done: "✅ Klar",
  error: "❌ Fel",
  cancelled: "🚫 Avbruten",
};

let queuePaused = false;
let lastKnownDoneCount = 0;

function renderQueueSteps(steps) {
  return (steps || [])
    .map((s) => {
      const icon = QUEUE_STEP_ICONS[s.status] || "⏳";
      const fillClass = ["done", "error", "skipped"].includes(s.status) ? s.status : "";
      return `
        <div class="step-row">
          <span class="step-label">${icon} ${escapeHtml(s.label)}</span>
          <span class="step-percent">${s.percent}%</span>
        </div>
        <div class="progress-bar-track small">
          <div class="progress-bar-fill ${fillClass}" style="width: ${s.percent}%;"></div>
        </div>`;
    })
    .join("");
}

function renderQueueList(items) {
  const list = document.getElementById("queueList");
  if (!items.length) {
    list.innerHTML = `<li class="queue-empty">Inget i kön just nu.</li>`;
    return;
  }

  list.innerHTML = items
    .map((it) => {
      const percent = it.overall_percent || 0;
      const fillClass = ["error", "done", "cancelled"].includes(it.status) ? it.status : "";
      const kindLabel = it.kind === "bulk" ? "CSV" : "Manuell";
      const statusLabel = QUEUE_STATUS_LABELS[it.status] || "";

      let body = "";
      let actions = "";
      if (it.status === "running") {
        body = `<div class="queue-steps">${renderQueueSteps(it.steps)}</div>`;
        actions = `<button type="button" class="queue-cancel-btn" data-job-id="${it.job_id}">🚫 Avbryt</button>`;
      } else if (it.status === "done" && it.result) {
        const tagsHtml = (it.result.tags || [])
          .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`)
          .join(" ");
        body = `
          <div class="queue-item-result">
            <strong>${escapeHtml(it.result.final_title)}</strong><br>
            <a href="${it.result.episode_url}" target="_blank">${it.result.episode_url}</a>
            ${tagsHtml ? `<div>${tagsHtml}</div>` : ""}
          </div>`;
      } else if (it.status === "error" || it.status === "cancelled") {
        body = `<div class="queue-item-error">${escapeHtml(it.error || "Okänt fel")}</div>`;
      }

      if (it.status === "queued") {
        actions = `<button type="button" class="queue-prioritize-btn" data-queue-id="${it.queue_id}">⬆ Prioritera</button>`;
      }
      if (it.status !== "running") {
        actions += `<button type="button" class="queue-remove-btn" data-queue-id="${it.queue_id}">✕ Ta bort</button>`;
      }

      return `
        <li class="queue-item">
          <div class="step-row">
            <span class="step-label">${escapeHtml(it.filename)} - ${escapeHtml(it.speaker)}<span class="queue-kind">${kindLabel}</span></span>
            <span class="step-percent">${percent}%</span>
          </div>
          <div class="progress-bar-track small">
            <div class="progress-bar-fill ${fillClass}" style="width: ${percent}%;"></div>
          </div>
          <div class="queue-status">${statusLabel}</div>
          ${body}
          ${actions ? `<div class="queue-item-actions">${actions}</div>` : ""}
        </li>`;
    })
    .join("");
}

// Knapparna skapas om vid varje omritning av listan (innerHTML), så klick
// hanteras med händelsedelegering på den stabila listcontainern istället
// för att binda om en lyssnare per knapp varje gång.
document.getElementById("queueList").addEventListener("click", async (e) => {
  const cancelBtn = e.target.closest(".queue-cancel-btn");
  const removeBtn = e.target.closest(".queue-remove-btn");
  const prioritizeBtn = e.target.closest(".queue-prioritize-btn");

  if (cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Avbryter...";
    try {
      await fetch(`/api/queue/cancel/${cancelBtn.dataset.jobId}`, { method: "POST" });
    } finally {
      loadQueue();
    }
  } else if (removeBtn) {
    removeBtn.disabled = true;
    try {
      const res = await fetch(`/api/queue/${removeBtn.dataset.queueId}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json();
        alert(err.detail || "Kunde inte ta bort objektet.");
      }
    } finally {
      loadQueue();
    }
  } else if (prioritizeBtn) {
    prioritizeBtn.disabled = true;
    try {
      await fetch(`/api/queue/prioritize/${prioritizeBtn.dataset.queueId}`, { method: "POST" });
    } finally {
      loadQueue();
    }
  }
});

document.getElementById("queueClearErrorsBtn").addEventListener("click", async () => {
  try {
    await fetch("/api/queue/clear-errors", { method: "POST" });
  } finally {
    loadQueue();
  }
});

document.getElementById("queueClearAllBtn").addEventListener("click", async () => {
  if (!confirm("Rensa hela kön? Objekt som redan bearbetas påverkas inte, men allt annat (väntande, klara och misslyckade) tas bort från listan.")) {
    return;
  }
  try {
    await fetch("/api/queue/clear", { method: "POST" });
  } finally {
    loadQueue();
  }
});

async function loadQueue() {
  try {
    const res = await fetch("/api/queue");
    if (!res.ok) return;
    const state = await res.json();

    queuePaused = state.paused;
    document.getElementById("queueStateLabel").textContent = state.paused ? "pausad" : "kör";
    document.getElementById("queueToggleBtn").textContent = state.paused ? "▶ Starta" : "⏸ Pausa";

    renderQueueList(state.items || []);

    const doneCount = (state.items || []).filter((it) => it.status === "done").length;
    if (doneCount > lastKnownDoneCount) loadStats();
    lastKnownDoneCount = doneCount;
  } catch {
    // Kön är en kompletterande vy - fel här ska aldrig blockera resten av appen.
  }
}

document.getElementById("queueToggleBtn").addEventListener("click", async () => {
  const endpoint = queuePaused ? "/api/queue/resume" : "/api/queue/pause";
  try {
    await fetch(endpoint, { method: "POST" });
  } finally {
    loadQueue();
  }
});

setInterval(loadQueue, 1500);
loadQueue();
