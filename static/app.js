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
// Mörkt/ljust läge
// Systemets/webbläsarens inställning (prefers-color-scheme) styr som
// standard (ren CSS, se style.css) - knappen låter användaren uttryckligen
// välja ett läge istället, sparat i localStorage så det kommer ihåg sig.
// ---------------------------------------------------------------------------
const THEME_STORAGE_KEY = "predikan-theme";

function isDarkThemeActive() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "dark") return true;
  if (explicit === "light") return false;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function updateThemeToggleButton() {
  const btn = document.getElementById("themeToggleBtn");
  if (!btn) return;
  btn.textContent = isDarkThemeActive() ? "☀️" : "🌙";
}

document.getElementById("themeToggleBtn").addEventListener("click", () => {
  const next = isDarkThemeActive() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    // localStorage kan vara blockerat - valet gäller då bara för den här sidladdningen.
  }
  updateThemeToggleButton();
  if (wavesurfer) {
    // Vågformens färger sätts vid skapandet (se initWaveform) - måste
    // byggas om för att plocka upp det nya temats färger.
    initWaveform(currentFileId);
  }
});

updateThemeToggleButton();

// ---------------------------------------------------------------------------
// Flikar (Bearbeta predikningar / Hantera Spreaker)
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

function showTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.getElementById("tab-process").classList.toggle("hidden", tabId !== "tab-process");
  document.getElementById("tab-spreaker").classList.toggle("hidden", tabId !== "tab-spreaker");
  // Bearbetningskön/statistiken hör bara hemma på den första fliken - på
  // Hantera Spreaker-fliken får huvudkolumnen (tabellen) hela bredden istället.
  document.querySelector(".queue-sidebar").classList.toggle("hidden", tabId !== "tab-process");
}

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

  const dark = isDarkThemeActive();
  wavesurfer = WaveSurfer.create({
    container: "#waveform",
    waveColor: dark ? "#4a4d68" : "#c9c9ec",
    progressColor: dark ? "#8b7dff" : "#4a3aff",
    cursorColor: dark ? "#e7e8f0" : "#23243a",
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
// Hantera Spreaker: visa/redigera avsnitt som REDAN ligger på det riktiga
// kontot (till skillnad från resten av sidan, som bara publicerar NYA
// avsnitt). Fliken är dold i HTML tills /api/spreaker/status bekräftar att
// Spreaker är konfigurerat för hantering (se routers/spreaker_episodes.py -
// backend litar aldrig bara på att fliken är dold, samma kontroll görs där).
//
// Listan är en lokal cache (modules/spreaker_episode_store.py) för
// snabbhets skull - "Hämta från Spreaker" gör det enda riktiga API-anropet,
// sortering sker sen helt i minnet utan nya anrop.
// ---------------------------------------------------------------------------
let spreakerEpisodes = [];
let spreakerSort = { field: "published_at", dir: "desc" };
const spreakerDirty = new Set();
const SPREAKER_NUMERIC_FIELDS = new Set(["duration_seconds", "plays_count"]);

function extractSpeaker(description) {
  if (!description) return null;
  const matches = [...description.matchAll(/^Talare:\s*(.+)$/gim)];
  if (!matches.length) return null;
  const last = matches[matches.length - 1][1].trim();
  return last || null;
}

async function loadSpreakerStatus() {
  try {
    const res = await fetch("/api/spreaker/status");
    if (!res.ok) return;
    const data = await res.json();
    if (data.configured) {
      document.getElementById("spreakerTabBtn").classList.remove("hidden");
      loadSpreakerEpisodes();
    }
  } catch {
    // Spreaker-hantering är en extra funktion - fel här ska inte blockera resten av appen.
  }
}

async function loadSpreakerEpisodes() {
  try {
    const res = await fetch("/api/spreaker/episodes");
    if (!res.ok) return;
    const data = await res.json();
    spreakerEpisodes = data.items || [];
    spreakerDirty.clear();
    sortSpreakerEpisodes();
    renderSpreakerTable();
  } catch {
    // Tyst - "Hämta från Spreaker"-knappen visar fel explicit vid ett faktiskt hämtningsförsök.
  }
}

function sortSpreakerEpisodes() {
  const { field, dir } = spreakerSort;
  if (field === "none") return;
  const mult = dir === "asc" ? 1 : -1;
  spreakerEpisodes.sort((a, b) => {
    let av;
    let bv;
    if (field === "speaker") {
      av = extractSpeaker(a.description) || "";
      bv = extractSpeaker(b.description) || "";
    } else {
      av = a[field];
      bv = b[field];
    }
    if (SPREAKER_NUMERIC_FIELDS.has(field)) {
      av = av ?? -Infinity;
      bv = bv ?? -Infinity;
      return (av - bv) * mult;
    }
    av = (av || "").toString().toLowerCase();
    bv = (bv || "").toString().toLowerCase();
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return 0;
  });
}

function renderSpreakerTable() {
  const body = document.getElementById("spreakerTableBody");
  if (!spreakerEpisodes.length) {
    body.innerHTML = `<tr><td colspan="6" class="queue-empty">Inget hämtat ännu - klicka "Hämta från Spreaker".</td></tr>`;
    return;
  }

  body.innerHTML = spreakerEpisodes
    .map((ep) => {
      const publishedLabel = ep.published_at ? new Date(ep.published_at.replace(" ", "T") + "Z").toLocaleDateString("sv-SE") : "-";
      const durationLabel = ep.duration_seconds ? formatDuration(ep.duration_seconds) : "-";
      const playsLabel = ep.plays_count != null ? ep.plays_count : "-";
      const speaker = extractSpeaker(ep.description) || "-";
      const dirtyClass = spreakerDirty.has(ep.episode_id) ? " dirty" : "";
      const retranscribeToggle = ep.has_transcript
        ? `<label class="spreaker-retranscribe-toggle"><input type="checkbox" class="spreaker-retranscribe-checkbox"> Transkribera om</label>`
        : "";
      return `
        <tr class="spreaker-row${dirtyClass}" data-episode-id="${ep.episode_id}">
          <td>
            <input type="text" class="spreaker-title-input" value="${escapeHtml(ep.title || "")}">
            <div class="spreaker-regen-row">
              <button type="button" class="spreaker-regen-btn" data-field="title" title="Generera om titel med AI">🤖 Titel</button>
              ${retranscribeToggle}
            </div>
            <div class="spreaker-regen-status" data-status-for="title"></div>
          </td>
          <td class="spreaker-speaker-cell">${escapeHtml(speaker)}</td>
          <td>${publishedLabel}</td>
          <td>${durationLabel}</td>
          <td>${playsLabel}</td>
          <td>
            <textarea class="spreaker-description-input" rows="2">${escapeHtml(ep.description || "")}</textarea>
            <div class="spreaker-regen-row">
              <button type="button" class="spreaker-regen-btn" data-field="description" title="Generera om beskrivning med AI">🤖 Beskrivning</button>
            </div>
            <div class="spreaker-regen-status" data-status-for="description"></div>
          </td>
        </tr>`;
    })
    .join("");
}

document.querySelectorAll("#spreakerTable th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const field = th.dataset.sort;
    if (field === "none") return;
    spreakerSort = spreakerSort.field === field
      ? { field, dir: spreakerSort.dir === "asc" ? "desc" : "asc" }
      : { field, dir: "asc" };

    document.querySelectorAll("#spreakerTable th[data-sort]").forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
    th.classList.add(spreakerSort.dir === "asc" ? "sort-asc" : "sort-desc");

    sortSpreakerEpisodes();
    renderSpreakerTable();
  });
});

// Redigering fångas löpande (input-event) istället för vid submit, så
// Talare-kolumnen kan uppdateras LIVE när beskrivningen redigeras, och så
// varje ändrad rad kan markeras "dirty" direkt.
document.getElementById("spreakerTableBody").addEventListener("input", (e) => {
  const row = e.target.closest(".spreaker-row");
  if (!row) return;
  const episodeId = parseInt(row.dataset.episodeId, 10);
  const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
  if (!ep) return;

  if (e.target.classList.contains("spreaker-title-input")) {
    ep.title = e.target.value;
  } else if (e.target.classList.contains("spreaker-description-input")) {
    ep.description = e.target.value;
    const speakerCell = row.querySelector(".spreaker-speaker-cell");
    if (speakerCell) speakerCell.textContent = extractSpeaker(ep.description) || "-";
  }

  if (!spreakerDirty.has(episodeId)) {
    spreakerDirty.add(episodeId);
    row.classList.add("dirty");
  }
  document.getElementById("spreakerSaveBtn").disabled = spreakerDirty.size === 0;
});

// "Generera om": lägger ett jobb i samma bearbetningskö som resten av
// appen (services/pipeline.py:_run_regenerate_job) och pollar det tills
// det är klart - precis som kösidopanelen redan gör, fast bara för DETTA
// jobb (kösidopanelen är medvetet dold på den här fliken, se showTab).
// Resultatet fylls bara i redigeringsfälten (markerat "dirty") - sparas
// INTE till Spreaker förrän användaren själv klickar "Spara ändringar".
document.getElementById("spreakerTableBody").addEventListener("click", async (e) => {
  const btn = e.target.closest(".spreaker-regen-btn");
  if (!btn) return;
  const row = btn.closest(".spreaker-row");
  const episodeId = parseInt(row.dataset.episodeId, 10);
  const field = btn.dataset.field; // "title" | "description"
  const retranscribeCheckbox = row.querySelector(".spreaker-retranscribe-checkbox");
  const forceRetranscribe = retranscribeCheckbox ? retranscribeCheckbox.checked : false;
  const statusEl = row.querySelector(`.spreaker-regen-status[data-status-for="${field}"]`);

  btn.disabled = true;
  if (statusEl) {
    statusEl.textContent = "⏳ Köar...";
    statusEl.className = "spreaker-regen-status";
  }

  try {
    const res = await fetch(`/api/spreaker/episodes/${episodeId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        regenerate_title: field === "title",
        regenerate_description: field === "description",
        force_retranscribe: forceRetranscribe,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Kunde inte starta generering.");
    pollRegenerateJob(data.job_id, episodeId, statusEl, btn);
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = `❌ ${err.message}`;
      statusEl.className = "spreaker-regen-status error";
    }
    btn.disabled = false;
  }
});

function pollRegenerateJob(jobId, episodeId, statusEl, btn) {
  const poll = async () => {
    let data;
    try {
      const res = await fetch(`/api/process/status/${jobId}`);
      if (!res.ok) throw new Error("Statusanrop misslyckades.");
      data = await res.json();
    } catch (err) {
      if (statusEl) {
        statusEl.textContent = `❌ ${err.message}`;
        statusEl.className = "spreaker-regen-status error";
      }
      if (btn) btn.disabled = false;
      return;
    }

    if (data.status === "running" || data.status === "queued") {
      if (statusEl) statusEl.textContent = `⏳ ${data.overall_percent || 0}%...`;
      setTimeout(poll, 1500);
      return;
    }

    if (data.status === "done") {
      const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
      if (ep) {
        if (data.result.title != null) ep.title = data.result.title;
        if (data.result.description != null) ep.description = data.result.description;
        ep.has_transcript = true;
        spreakerDirty.add(episodeId);
        document.getElementById("spreakerSaveBtn").disabled = false;
      }
      renderSpreakerTable();
      const globalStatus = document.getElementById("spreakerStatus");
      globalStatus.textContent = `✅ Nytt förslag klart för "${(ep && ep.title) || episodeId}" - granska och spara.`;
      globalStatus.className = "status success";
      return;
    }

    // error/cancelled
    if (statusEl) {
      statusEl.textContent = `❌ ${data.error || "Misslyckades"}`;
      statusEl.className = "spreaker-regen-status error";
    }
    if (btn) btn.disabled = false;
  };
  poll();
}

document.getElementById("spreakerFetchBtn").addEventListener("click", async () => {
  const btn = document.getElementById("spreakerFetchBtn");
  const status = document.getElementById("spreakerStatus");
  btn.disabled = true;
  status.textContent = "Hämtar från Spreaker...";
  status.className = "status";
  try {
    const res = await fetch("/api/spreaker/episodes/fetch", { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Kunde inte hämta från Spreaker.");
    }
    spreakerEpisodes = data.items || [];
    spreakerDirty.clear();
    sortSpreakerEpisodes();
    renderSpreakerTable();
    document.getElementById("spreakerSaveBtn").disabled = true;
    status.textContent = `✅ ${spreakerEpisodes.length} avsnitt hämtade.`;
    status.className = "status success";
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "status error";
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("spreakerSaveBtn").addEventListener("click", async () => {
  const btn = document.getElementById("spreakerSaveBtn");
  const status = document.getElementById("spreakerStatus");
  const idsToSave = Array.from(spreakerDirty);
  if (!idsToSave.length) return;

  btn.disabled = true;
  status.textContent = `Sparar ${idsToSave.length} ändrade avsnitt...`;
  status.className = "status";

  let savedCount = 0;
  let failedCount = 0;
  for (const episodeId of idsToSave) {
    const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
    if (!ep) continue;
    try {
      const res = await fetch(`/api/spreaker/episodes/${episodeId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: ep.title, description: ep.description }),
      });
      if (!res.ok) throw new Error();
      spreakerDirty.delete(episodeId);
      savedCount++;
    } catch {
      failedCount++;
    }
  }

  renderSpreakerTable();
  btn.disabled = spreakerDirty.size === 0;
  status.textContent = failedCount
    ? `⚠️ ${savedCount} sparade, ${failedCount} misslyckades (försök igen).`
    : `✅ ${savedCount} avsnitt sparade.`;
  status.className = failedCount ? "status error" : "status success";
});

loadSpreakerStatus();

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

function renderEtaTimer(it) {
  if (!it.estimated_completion_at) {
    return `<div class="queue-eta queue-eta-unknown">⏱️ Ingen tidsuppskattning tillgänglig än (ingen bearbetningshistorik).</div>`;
  }
  const remainingMs = new Date(it.estimated_completion_at).getTime() - Date.now();
  if (remainingMs <= 0) {
    return `<div class="queue-eta">⏱️ Tar längre än beräknat...</div>`;
  }
  return `<div class="queue-eta">⏱️ Beräknat klart om ~${formatDuration(remainingMs / 1000)}</div>`;
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
      const kindLabel = { bulk: "CSV", regenerate: "AI" }[it.kind] || "Manuell";
      const statusLabel = QUEUE_STATUS_LABELS[it.status] || "";

      let body = "";
      let actions = "";
      if (it.status === "running") {
        body = `${renderEtaTimer(it)}<div class="queue-steps">${renderQueueSteps(it.steps)}</div>`;
        actions = `<button type="button" class="queue-cancel-btn" data-job-id="${it.job_id}">🚫 Avbryt</button>`;
      } else if (it.status === "done" && it.kind === "regenerate" && it.result) {
        body = `
          <div class="queue-item-result">
            Nytt förslag genererat - granska och spara i "Hantera Spreaker"-fliken.
          </div>`;
      } else if (it.status === "done" && it.result) {
        const tagsHtml = (it.result.tags || [])
          .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`)
          .join(" ");
        body = `
          <div class="queue-item-result">
            <strong>${escapeHtml(it.result.final_title)}</strong><br>
            <a href="${escapeHtml(it.result.episode_url)}" target="_blank">${escapeHtml(it.result.episode_url)}</a>
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

document.getElementById("queueClearDoneBtn").addEventListener("click", async () => {
  try {
    await fetch("/api/queue/clear-done", { method: "POST" });
  } finally {
    loadQueue();
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
