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
const stepProcessing = document.getElementById("step-processing");
const stepResult = document.getElementById("step-result");

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
// STEG 3-6: Formulär -> bearbetning -> resultat
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

  stepProcessing.classList.remove("hidden");
  stepResult.classList.add("hidden");
  document.getElementById("processBtn").disabled = true;
  document.getElementById("processingLog").innerHTML = "";
  renderOverallProgress(0);
  stepProcessing.scrollIntoView({ behavior: "smooth" });

  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Bearbetning kunde inte startas");
    }

    const { job_id } = await res.json();
    pollJobStatus(job_id);
  } catch (err) {
    renderProcessingError(err.message);
    document.getElementById("processBtn").disabled = false;
  }
});

// Ikon per stegstatus - används för att rendera bearbetningsloggen live.
const STEP_ICONS = {
  pending: "⏳",
  running: "⚙️",
  done: "✅",
  skipped: "⏭️",
  error: "❌",
};

function renderSteps(steps) {
  const log = document.getElementById("processingLog");
  log.innerHTML = steps
    .map((s) => {
      const icon = STEP_ICONS[s.status] || "⏳";
      const runningNote = s.status === "running" ? " <em>(pågår...)</em>" : "";
      const skippedNote = s.status === "skipped" ? " <em>(hoppades över)</em>" : "";
      const fillClass = ["done", "error", "skipped"].includes(s.status) ? s.status : "";
      return `
        <li>
          <div class="step-row">
            <span class="step-label">${icon} ${escapeHtml(s.label)}${runningNote}${skippedNote}</span>
            <span class="step-percent">${s.percent}%</span>
          </div>
          <div class="progress-bar-track small">
            <div class="progress-bar-fill ${fillClass}" style="width: ${s.percent}%;"></div>
          </div>
        </li>`;
    })
    .join("");
}

function renderOverallProgress(percent) {
  document.getElementById("overallProgressFill").style.width = `${percent}%`;
  document.getElementById("overallProgressLabel").textContent = `${percent}%`;
}

function renderProcessingError(message) {
  const log = document.getElementById("processingLog");
  log.innerHTML += `<li>❌ ${escapeHtml(message)}</li>`;
}

function pollJobStatus(jobId) {
  const intervalId = setInterval(async () => {
    try {
      const res = await fetch(`/api/process/status/${jobId}`);
      if (!res.ok) {
        clearInterval(intervalId);
        renderProcessingError("Kunde inte hämta bearbetningsstatus.");
        document.getElementById("processBtn").disabled = false;
        return;
      }

      const job = await res.json();
      renderSteps(job.steps);
      renderOverallProgress(job.overall_percent);

      if (job.status === "done") {
        clearInterval(intervalId);
        renderOverallProgress(100);
        renderResult(job.result);
        document.getElementById("processBtn").disabled = false;
        loadStats();
      } else if (job.status === "error") {
        clearInterval(intervalId);
        renderProcessingError(job.error || "Ett okänt fel inträffade.");
        document.getElementById("processBtn").disabled = false;
      }
    } catch (err) {
      clearInterval(intervalId);
      renderProcessingError(err.message);
      document.getElementById("processBtn").disabled = false;
    }
  }, 1000);
}

function renderResult(result) {
  stepProcessing.classList.add("hidden");
  stepResult.classList.remove("hidden");

  const tagsHtml = (result.tags || [])
    .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`)
    .join(" ");

  const simulatedNote = result.simulated
    ? `<p style="color:#e0a020;"><em>⚠️ Spreaker-publicering är simulerad (inga riktiga API-nycklar konfigurerade).</em></p>`
    : "";

  let publishNote;
  if (result.scheduled) {
    publishNote = `<p><strong>Schemalagd publicering:</strong> ${escapeHtml(formatPublishDate(result.publish_date))}</p>`;
  } else if (result.backdated) {
    publishNote = `<p><strong>Bakåtdaterad till:</strong> ${escapeHtml(formatPublishDate(result.publish_date))}</p>`;
  } else {
    publishNote = `<p><strong>Publicerad:</strong> Direkt (dagens datum)</p>`;
  }

  const emailNote = result.email_sent
    ? `<p>📧 Bekräftelsemail skickat.</p>`
    : `<p>📧 E-post ej konfigurerat - se sammanfattningen nedan.</p>`;

  document.getElementById("resultSummary").innerHTML = `
    ${simulatedNote}
    <p><strong>Titel:</strong> ${escapeHtml(result.final_title)}</p>
    <p><strong>Talare:</strong> ${escapeHtml(result.speaker)}</p>
    <p><strong>Beskrivning:</strong></p>
    <div class="description-block">${escapeHtml(result.final_description)}</div>
    <p><strong>Taggar:</strong> ${tagsHtml || "-"}</p>
    ${publishNote}
    <p><strong>Länk till avsnitt:</strong> <a href="${result.episode_url}" target="_blank">${result.episode_url}</a></p>
    ${emailNote}
  `;

  stepResult.scrollIntoView({ behavior: "smooth" });
}

function formatPublishDate(isoString) {
  if (!isoString) return "-";
  const date = new Date(isoString);
  return date.toLocaleString("sv-SE", { dateStyle: "long", timeStyle: "short" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Bulk-import via CSV
// ---------------------------------------------------------------------------
const BULK_STEP_ICONS = {
  running: "⚙️",
  done: "✅",
  error: "❌",
};

let bulkPollIntervalId = null;

document.getElementById("bulkImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("bulkCsvInput");
  const file = fileInput.files[0];
  const bulkStatus = document.getElementById("bulkImportStatus");
  const bulkList = document.getElementById("bulkImportList");

  if (!file) {
    bulkStatus.textContent = "Välj en CSV-fil först.";
    bulkStatus.className = "status error";
    return;
  }

  bulkStatus.textContent = "Läser in och validerar CSV-filen...";
  bulkStatus.className = "status";
  bulkList.innerHTML = "";
  document.getElementById("bulkImportBtn").disabled = true;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/bulk-import", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Import misslyckades");
    }

    bulkStatus.textContent = `✅ ${data.items.length} predikning(ar) i kö för bearbetning...`;
    bulkStatus.className = "status success";
    renderBulkItems(data.items.map((it) => ({ ...it, status: "running", overall_percent: 0 })));

    if (bulkPollIntervalId) clearInterval(bulkPollIntervalId);
    pollBulkStatus(data.batch_id);
  } catch (err) {
    bulkStatus.textContent = `❌ ${err.message}`;
    bulkStatus.className = "status error";
    document.getElementById("bulkImportBtn").disabled = false;
  }
});

function pollBulkStatus(batchId) {
  bulkPollIntervalId = setInterval(async () => {
    try {
      const res = await fetch(`/api/bulk-import/status/${batchId}`);
      if (!res.ok) {
        clearInterval(bulkPollIntervalId);
        return;
      }
      const batch = await res.json();
      renderBulkItems(batch.items);

      if (batch.status === "done") {
        clearInterval(bulkPollIntervalId);
        document.getElementById("bulkImportBtn").disabled = false;
        document.getElementById("bulkImportStatus").textContent = "✅ Bulkimport klar.";
        loadStats();
      }
    } catch {
      clearInterval(bulkPollIntervalId);
      document.getElementById("bulkImportBtn").disabled = false;
    }
  }, 1500);
}

function renderBulkItems(items) {
  const list = document.getElementById("bulkImportList");
  list.innerHTML = items
    .map((it) => {
      const icon = BULK_STEP_ICONS[it.status] || "⏳";
      const percent = it.overall_percent || 0;
      const errorNote = it.status === "error" && it.error ? `<div class="hint">${escapeHtml(it.error)}</div>` : "";
      const linkNote = it.status === "done" && it.result
        ? `<div class="hint"><a href="${it.result.episode_url}" target="_blank">${it.result.episode_url}</a></div>`
        : "";
      return `
        <li>
          <div class="step-row">
            <span class="step-label">${icon} ${escapeHtml(it.filename)} - ${escapeHtml(it.speaker)}</span>
            <span class="step-percent">${percent}%</span>
          </div>
          <div class="progress-bar-track small">
            <div class="progress-bar-fill ${it.status === "error" ? "error" : it.status === "done" ? "done" : ""}" style="width: ${percent}%;"></div>
          </div>
          ${errorNote}
          ${linkNote}
        </li>`;
    })
    .join("");
}
