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
  document.getElementById("tab-setup").classList.toggle("hidden", tabId !== "tab-setup");
  // Bearbetningskön/statistiken hör bara hemma på den första fliken - på
  // Hantera Spreaker- och Inställningar-flikarna får huvudkolumnen hela bredden istället.
  document.querySelector(".queue-sidebar").classList.toggle("hidden", tabId !== "tab-process");
  if (tabId === "tab-setup") loadSetupConfig();
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
const SPREAKER_NUMERIC_FIELDS = new Set(["duration_seconds", "plays_count", "archived"]);

// Paginering sker helt i minnet (hela listan finns redan i spreakerEpisodes)
// - sortering gäller alltid HELA listan, sen visas bara aktuell sida.
// Sidstorleken kommer ihåg sig per webbläsare; 0 = visa alla.
const SPREAKER_PAGE_SIZE_KEY = "predikan-spreaker-page-size";
let spreakerPage = 1;
let spreakerPageSize = 25;
try {
  const saved = parseInt(localStorage.getItem(SPREAKER_PAGE_SIZE_KEY), 10);
  if (!Number.isNaN(saved)) spreakerPageSize = saved;
} catch {
  // localStorage kan vara blockerat - standardvärdet gäller då.
}
document.getElementById("spreakerPageSize").value = String(spreakerPageSize);

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
    if (data.configured || data.archive_available) {
      document.getElementById("spreakerTabBtn").classList.remove("hidden");
    }
    document.getElementById("spreakerManageCard").classList.toggle("hidden", !data.configured);
    document.getElementById("archiveCard").classList.toggle("hidden", !data.archive_available);
    if (data.configured) loadSpreakerEpisodes();
    if (data.archive_available) loadArchiveStatus();
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
    spreakerEpisodes.forEach(rememberSavedValues);
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

function renderSpreakerPaginator() {
  const total = spreakerEpisodes.length;
  const pageCount = spreakerPageSize ? Math.max(1, Math.ceil(total / spreakerPageSize)) : 1;
  spreakerPage = Math.min(Math.max(1, spreakerPage), pageCount);
  const first = spreakerPageSize ? (spreakerPage - 1) * spreakerPageSize : 0;
  const last = spreakerPageSize ? Math.min(total, first + spreakerPageSize) : total;

  document.getElementById("spreakerPaginator").classList.toggle("hidden", !total);
  document.getElementById("spreakerPageInfo").textContent =
    `Sida ${spreakerPage} av ${pageCount} · visar ${total ? first + 1 : 0}–${last} av ${total}`;
  document.getElementById("spreakerPrevBtn").disabled = spreakerPage <= 1;
  document.getElementById("spreakerNextBtn").disabled = spreakerPage >= pageCount;
  return spreakerEpisodes.slice(first, last);
}

// Kommer ihåg vad som faktiskt ligger sparat på Spreaker (saved_title/
// saved_description), så ett AI-förslag kan visas bredvid den nuvarande
// versionen och ångras. ep.suggested = { title, description } markerar
// vilka fält som just nu innehåller ett ogranskat AI-förslag.
function rememberSavedValues(ep) {
  ep.saved_title = ep.title || "";
  ep.saved_description = ep.description || "";
  ep.suggested = {};
}

function isEpisodeUnchanged(ep) {
  return (ep.title || "") === ep.saved_title && (ep.description || "") === ep.saved_description;
}

// Visar den nuvarande (sparade) versionen bredvid ett AI-förslag.
function renderSuggestionCompare(ep, field) {
  const saved = field === "title" ? ep.saved_title : ep.saved_description;
  const current = field === "title" ? ep.title : ep.description;
  if (!ep.suggested || !ep.suggested[field] || saved === (current || "")) return "";
  return `
    <div class="spreaker-compare-current">
      <div class="spreaker-compare-label">Nuvarande</div>
      <div class="spreaker-compare-text">${escapeHtml(saved) || "<em>(tom)</em>"}</div>
      <button type="button" class="spreaker-revert-btn" data-field="${field}">↩️ Behåll nuvarande</button>
    </div>`;
}

function renderSuggestionField(ep, field, inputHtml) {
  const compare = renderSuggestionCompare(ep, field);
  if (!compare) return inputHtml;
  return `
    <div class="spreaker-compare">
      ${compare}
      <div class="spreaker-compare-suggestion">
        <div class="spreaker-compare-label">🤖 Förslag</div>
        ${inputHtml}
      </div>
    </div>`;
}

async function saveSpreakerEpisode(ep) {
  const res = await fetch(`/api/spreaker/episodes/${ep.episode_id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: ep.title, description: ep.description }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Kunde inte spara.");
  }
  rememberSavedValues(ep);
  spreakerDirty.delete(ep.episode_id);
}

function renderSpreakerTable() {
  const body = document.getElementById("spreakerTableBody");
  const pageItems = renderSpreakerPaginator();
  document.getElementById("spreakerSaveBtn").disabled = spreakerDirty.size === 0;
  if (!spreakerEpisodes.length) {
    body.innerHTML = `<tr><td colspan="8" class="queue-empty">Inget hämtat ännu - klicka "Hämta från Spreaker".</td></tr>`;
    return;
  }

  body.innerHTML = pageItems
    .map((ep) => {
      const publishedLabel = ep.published_at ? new Date(ep.published_at.replace(" ", "T") + "Z").toLocaleDateString("sv-SE") : "-";
      const durationLabel = ep.duration_seconds ? formatDuration(ep.duration_seconds) : "-";
      const playsLabel = ep.plays_count != null ? ep.plays_count : "-";
      const speaker = extractSpeaker(ep.description) || "-";
      const dirtyClass = spreakerDirty.has(ep.episode_id) ? " dirty" : "";
      const archiveLabel = [
        ep.archived ? `<span title="Ljudet finns i det lokala arkivet">🗄️</span>` : "",
        ep.has_transcript ? `<span title="Transkript finns sparat">📝</span>` : "",
      ].join(" ").trim() || "-";
      const retranscribeToggle = ep.has_transcript
        ? `<label class="spreaker-retranscribe-toggle"><input type="checkbox" class="spreaker-retranscribe-checkbox"> Transkribera om</label>`
        : "";
      return `
        <tr class="spreaker-row${dirtyClass}" data-episode-id="${ep.episode_id}">
          <td>
            ${renderSuggestionField(ep, "title", `<input type="text" class="spreaker-title-input" value="${escapeHtml(ep.title || "")}">`)}
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
          <td class="spreaker-archive-cell">${archiveLabel}</td>
          <td>
            ${renderSuggestionField(ep, "description", `<textarea class="spreaker-description-input" rows="${ep.suggested && ep.suggested.description ? 6 : 2}">${escapeHtml(ep.description || "")}</textarea>`)}
            <div class="spreaker-regen-row">
              <button type="button" class="spreaker-regen-btn" data-field="description" title="Generera om beskrivning med AI">🤖 Beskrivning</button>
            </div>
            <div class="spreaker-regen-status" data-status-for="description"></div>
          </td>
          <td>
            <button type="button" class="spreaker-row-save-btn" ${spreakerDirty.has(ep.episode_id) ? "" : "disabled"}>💾 Spara</button>
            <div class="spreaker-row-save-status"></div>
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
    spreakerPage = 1;
    renderSpreakerTable();
  });
});

document.getElementById("spreakerPrevBtn").addEventListener("click", () => {
  spreakerPage -= 1;
  renderSpreakerTable();
});

document.getElementById("spreakerNextBtn").addEventListener("click", () => {
  spreakerPage += 1;
  renderSpreakerTable();
});

document.getElementById("spreakerPageSize").addEventListener("change", (e) => {
  spreakerPageSize = parseInt(e.target.value, 10) || 0;
  spreakerPage = 1;
  try {
    localStorage.setItem(SPREAKER_PAGE_SIZE_KEY, String(spreakerPageSize));
  } catch {
    // Ignoreras - valet gäller då bara tills sidan laddas om.
  }
  renderSpreakerTable();
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
  row.querySelector(".spreaker-row-save-btn").disabled = false;
  document.getElementById("spreakerSaveBtn").disabled = spreakerDirty.size === 0;
});

// Per rad: "↩️ Behåll nuvarande" (ångra ett AI-förslag) och "💾 Spara".
document.getElementById("spreakerTableBody").addEventListener("click", async (e) => {
  const revertBtn = e.target.closest(".spreaker-revert-btn");
  const saveBtn = e.target.closest(".spreaker-row-save-btn");
  if (!revertBtn && !saveBtn) return;
  const row = e.target.closest(".spreaker-row");
  const episodeId = parseInt(row.dataset.episodeId, 10);
  const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
  if (!ep) return;

  if (revertBtn) {
    const field = revertBtn.dataset.field;
    ep[field] = field === "title" ? ep.saved_title : ep.saved_description;
    ep.suggested[field] = false;
    if (isEpisodeUnchanged(ep)) spreakerDirty.delete(episodeId);
    renderSpreakerTable();
    return;
  }

  const statusEl = row.querySelector(".spreaker-row-save-status");
  saveBtn.disabled = true;
  statusEl.className = "spreaker-row-save-status";
  statusEl.textContent = "Sparar...";
  try {
    await saveSpreakerEpisode(ep);
    renderSpreakerTable();
    const newStatus = document.querySelector(`.spreaker-row[data-episode-id="${episodeId}"] .spreaker-row-save-status`);
    if (newStatus) {
      newStatus.className = "spreaker-row-save-status success";
      newStatus.textContent = "✅ Sparad";
    }
  } catch (err) {
    statusEl.className = "spreaker-row-save-status error";
    statusEl.textContent = `❌ ${err.message}`;
    saveBtn.disabled = false;
  }
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
        ep.suggested = ep.suggested || {};
        if (data.result.title != null) {
          ep.title = data.result.title;
          ep.suggested.title = true;
        }
        if (data.result.description != null) {
          ep.description = data.result.description;
          ep.suggested.description = true;
        }
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
    spreakerEpisodes.forEach(rememberSavedValues);
    spreakerDirty.clear();
    sortSpreakerEpisodes();
    spreakerPage = 1;
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
      await saveSpreakerEpisode(ep);
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
// Lokalt podd-arkiv (modules/podcast_archive.py). Körs i en egen
// bakgrundstråd på servern - här pollas bara statusen medan den pågår.
// ---------------------------------------------------------------------------
let archivePollTimer = null;
let archiveWasRunning = false;

// Hämtar om bara arkivinfon (🗄️/📝) för listan, utan att tappa osparade ändringar.
async function refreshSpreakerArchiveInfo() {
  try {
    const res = await fetch("/api/spreaker/episodes");
    if (!res.ok) return;
    const fresh = new Map(((await res.json()).items || []).map((ep) => [ep.episode_id, ep]));
    for (const ep of spreakerEpisodes) {
      const f = fresh.get(ep.episode_id);
      if (f) {
        ep.archived = f.archived;
        ep.has_transcript = f.has_transcript;
      }
    }
    renderSpreakerTable();
  } catch {
    // Tyst - kolumnen uppdateras nästa gång listan laddas.
  }
}

function formatBytes(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} kB`;
}

function renderArchiveStatus(s) {
  document.getElementById("archiveDir").textContent = s.archive_dir || "-";
  document.getElementById("archiveRunBtn").disabled = s.running;
  document.getElementById("archiveStopBtn").classList.toggle("hidden", !s.running);
  document.getElementById("archiveStopBtn").disabled = s.stopping;
  const track = document.getElementById("archiveProgressTrack");
  const fill = document.getElementById("archiveProgressFill");
  const status = document.getElementById("archiveStatus");
  track.classList.toggle("hidden", !s.running);
  status.className = "status";

  if (s.running) {
    const pct = s.total ? ((s.index - 1 + (s.bytes_total ? s.bytes_done / s.bytes_total : 0)) / s.total) * 100 : 0;
    fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    const bytes = s.bytes_done ? ` - ${formatBytes(s.bytes_done)}${s.bytes_total ? " av " + formatBytes(s.bytes_total) : ""}` : "";
    status.textContent = s.total
      ? `${s.stopping ? "Avbryter... " : ""}[${s.index}/${s.total}] ${s.current}${bytes}`
      : s.current;
    return;
  }
  if (!s.finished_at) {
    status.textContent = "";
    return;
  }
  if (s.error) {
    status.className = "status error";
    status.textContent = `❌ ${s.error}`;
    return;
  }
  const dl = s.downloaded ? ` (${formatBytes(s.downloaded_bytes)})` : "";
  let text = `Nedladdade: ${s.downloaded}${dl} · Fanns redan: ${s.skipped} · Misslyckade: ${s.failures.length}`;
  if (s.failures.length) text += "\n" + s.failures.map((f) => `- ${f}`).join("\n");
  status.className = s.failures.length ? "status error" : "status success";
  status.style.whiteSpace = "pre-line";
  status.textContent = (s.failures.length ? "⚠️ " : "✅ ") + text;
}

async function loadArchiveStatus() {
  try {
    const res = await fetch("/api/spreaker/archive/status");
    if (!res.ok) return;
    const s = await res.json();
    renderArchiveStatus(s);
    clearTimeout(archivePollTimer);
    if (s.running) {
      archivePollTimer = setTimeout(loadArchiveStatus, 1000);
      archiveWasRunning = true;
    } else if (archiveWasRunning) {
      // Nyss klar - uppdatera Arkiv-kolumnen i avsnittslistan (om den visas).
      archiveWasRunning = false;
      if (!document.getElementById("spreakerManageCard").classList.contains("hidden")) refreshSpreakerArchiveInfo();
    }
  } catch {
    // Tyst - nästa knapptryck visar ett eventuellt fel.
  }
}

document.getElementById("archiveRunBtn").addEventListener("click", async () => {
  const res = await fetch("/api/spreaker/archive/run", { method: "POST" });
  if (!res.ok && res.status !== 409) {
    const data = await res.json().catch(() => ({}));
    const status = document.getElementById("archiveStatus");
    status.className = "status error";
    status.textContent = `❌ ${data.detail || "Kunde inte starta arkiveringen."}`;
    return;
  }
  loadArchiveStatus();
});

document.getElementById("archiveStopBtn").addEventListener("click", async () => {
  await fetch("/api/spreaker/archive/stop", { method: "POST" });
  loadArchiveStatus();
});

// ---------------------------------------------------------------------------
// Inställningsguide (fliken ⚙️ Inställningar)
// Fyller i .env via GUI:t och guidar Spreaker-OAuth. Sparade värden skrivs
// till .env och läses om live av backend (se routers/setup.py + config.reload).
// ---------------------------------------------------------------------------
let discoveredSpreakerToken = null; // sätts av OAuth-utbytet/token-verifieringen

function setupStatus(message, ok) {
  const el = document.getElementById("setupStatus");
  el.textContent = message;
  el.className = ok === false ? "status error" : ok === true ? "status success" : "status";
}

async function loadSetupConfig() {
  try {
    const res = await fetch("/api/setup/config");
    if (!res.ok) return;
    const c = await res.json();

    document.getElementById("spShowSelect").innerHTML = "";
    document.getElementById("spSimulate").checked = c.spreaker_simulate;
    if (c.spreaker_show_id) {
      const sel = document.getElementById("spShowSelect");
      sel.innerHTML = `<option value="${escapeHtml(c.spreaker_show_id)}">Nuvarande: ${escapeHtml(c.spreaker_show_id)}</option>`;
      document.getElementById("spShowBox").hidden = false;
    }

    document.getElementById("openaiKey").placeholder = c.openai_api_key_set
      ? `Sparad (${c.openai_api_key_masked}) - lämna tomt för att behålla`
      : "sk-...";

    document.getElementById("useLocalWhisper").checked = c.use_local_whisper;
    setSelect("localWhisperModel", c.local_whisper_model);
    setSelect("whisperDevice", c.whisper_device);

    setSelect("aiProvider", c.ai_provider);
    document.getElementById("ollamaHost").value = c.ollama_host || "";
    document.getElementById("ollamaModel").value = c.ollama_model || "";

    document.getElementById("emailEnabled").checked = c.email_enabled;
    document.getElementById("smtpHost").value = c.smtp_host || "";
    document.getElementById("smtpPort").value = c.smtp_port || "";
    document.getElementById("smtpUser").value = c.smtp_user || "";
    document.getElementById("smtpPassword").placeholder = c.smtp_password_set
      ? "Sparat - lämna tomt för att behålla"
      : "app-lösenord";
    document.getElementById("notifyEmail").value = c.notify_email || "";

    document.getElementById("maxStored").value = c.max_stored_episodes || 0;
    setSelect("logLevel", c.log_level);
    document.getElementById("archiveDirInput").value = c.archive_dir || "";
    document.getElementById("archiveDirResolved").textContent = c.archive_dir_resolved
      ? `Sparas i: ${c.archive_dir_resolved}`
      : "";
  } catch {
    setupStatus("Kunde inte läsa inställningarna.", false);
  }
}

function setSelect(id, value) {
  const el = document.getElementById(id);
  if (el && value != null) el.value = value;
}

function populateShowSelect(shows) {
  const sel = document.getElementById("spShowSelect");
  if (!shows.length) {
    sel.innerHTML = `<option value="">(Inga shows hittades på kontot)</option>`;
  } else {
    sel.innerHTML = shows
      .map((s) => `<option value="${escapeHtml(String(s.show_id))}">${escapeHtml(s.title)} (${escapeHtml(String(s.show_id))})</option>`)
      .join("");
  }
  document.getElementById("spShowBox").hidden = false;
}

document.getElementById("spBuildUrlBtn").addEventListener("click", async () => {
  const clientId = document.getElementById("spClientId").value.trim();
  const redirectUri = document.getElementById("spRedirectUri").value.trim() || "http://localhost";
  if (!clientId) return setupStatus("Fyll i Client ID först.", false);
  try {
    const res = await fetch("/api/setup/spreaker/authorize-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, redirect_uri: redirectUri }),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Kunde inte skapa länken.", false);
    const link = document.getElementById("spAuthLink");
    link.href = data.url;
    link.textContent = data.url;
    document.getElementById("spAuthLinkWrap").hidden = false;
    setupStatus("Öppna länken, godkänn, och klistra tillbaka adressen nedan.", true);
  } catch {
    setupStatus("Nätverksfel när länken skulle skapas.", false);
  }
});

document.getElementById("spExchangeBtn").addEventListener("click", async () => {
  const body = {
    client_id: document.getElementById("spClientId").value.trim(),
    client_secret: document.getElementById("spClientSecret").value.trim(),
    redirect_uri: document.getElementById("spRedirectUri").value.trim() || "http://localhost",
    code: document.getElementById("spCode").value.trim(),
  };
  if (!body.client_id || !body.client_secret || !body.code) {
    return setupStatus("Fyll i Client ID, Client Secret och koden/URL:en.", false);
  }
  setupStatus("Byter kod mot token...", null);
  try {
    const res = await fetch("/api/setup/spreaker/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Utbytet misslyckades.", false);
    discoveredSpreakerToken = data.token;
    populateShowSelect(data.shows || []);
    const name = (data.user && data.user.fullname) || "okänd användare";
    setupStatus(`✅ Token hämtad (inloggad som ${name}). Välj ditt show och spara.`, true);
  } catch {
    setupStatus("Nätverksfel vid utbytet.", false);
  }
});

document.getElementById("spHaveTokenBtn").addEventListener("click", () => {
  const box = document.getElementById("spTokenBox");
  box.hidden = !box.hidden;
});

document.getElementById("spVerifyTokenBtn").addEventListener("click", async () => {
  const token = document.getElementById("spToken").value.trim();
  if (!token) return setupStatus("Klistra in en token först.", false);
  setupStatus("Verifierar token...", null);
  try {
    const res = await fetch("/api/setup/spreaker/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Verifieringen misslyckades.", false);
    discoveredSpreakerToken = token;
    populateShowSelect(data.shows || []);
    const name = (data.user && data.user.fullname) || "okänd användare";
    setupStatus(`✅ Token verifierad (inloggad som ${name}). Välj ditt show och spara.`, true);
  } catch {
    setupStatus("Nätverksfel vid verifieringen.", false);
  }
});

document.getElementById("spSaveBtn").addEventListener("click", async () => {
  const values = {
    SPREAKER_SIMULATE: document.getElementById("spSimulate").checked ? "true" : "false",
  };
  const showId = document.getElementById("spShowSelect").value;
  if (showId) values.SPREAKER_SHOW_ID = showId;
  if (discoveredSpreakerToken) values.SPREAKER_API_TOKEN = discoveredSpreakerToken;
  await saveSettings(values, "Spreaker-inställningar sparade.");
});

document.getElementById("openaiVerifyBtn").addEventListener("click", async () => {
  const key = document.getElementById("openaiKey").value.trim();
  if (!key) return setupStatus("Fyll i en nyckel att verifiera.", false);
  setupStatus("Verifierar OpenAI-nyckel...", null);
  try {
    const res = await fetch("/api/setup/openai/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json();
    setupStatus(res.ok ? "✅ OpenAI-nyckeln fungerar." : data.detail || "Nyckeln avvisades.", res.ok);
  } catch {
    setupStatus("Nätverksfel vid verifieringen.", false);
  }
});

document.getElementById("setupSaveAllBtn").addEventListener("click", async () => {
  const values = {
    USE_LOCAL_WHISPER: document.getElementById("useLocalWhisper").checked ? "true" : "false",
    LOCAL_WHISPER_MODEL: document.getElementById("localWhisperModel").value,
    WHISPER_DEVICE: document.getElementById("whisperDevice").value,
    AI_PROVIDER: document.getElementById("aiProvider").value,
    OLLAMA_HOST: document.getElementById("ollamaHost").value.trim(),
    OLLAMA_MODEL: document.getElementById("ollamaModel").value.trim(),
    EMAIL_ENABLED: document.getElementById("emailEnabled").checked ? "true" : "false",
    SMTP_HOST: document.getElementById("smtpHost").value.trim(),
    SMTP_PORT: document.getElementById("smtpPort").value.trim() || "587",
    SMTP_USER: document.getElementById("smtpUser").value.trim(),
    NOTIFY_EMAIL: document.getElementById("notifyEmail").value.trim(),
    MAX_STORED_EPISODES: document.getElementById("maxStored").value.trim() || "0",
    LOG_LEVEL: document.getElementById("logLevel").value,
    ARCHIVE_DIR: document.getElementById("archiveDirInput").value.trim() || "podcast_arkiv",
  };
  // Hemligheter skickas bara om användaren faktiskt skrivit något (annars
  // behåller backend det sparade värdet, se routers/setup.py).
  const openaiKey = document.getElementById("openaiKey").value.trim();
  if (openaiKey) values.OPENAI_API_KEY = openaiKey;
  const smtpPassword = document.getElementById("smtpPassword").value.trim();
  if (smtpPassword) values.SMTP_PASSWORD = smtpPassword;
  await saveSettings(values, "Alla inställningar sparade.");
});

async function saveSettings(values, successMessage) {
  setupStatus("Sparar...", null);
  try {
    const res = await fetch("/api/setup/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Kunde inte spara.", false);
    setupStatus(`✅ ${successMessage}`, true);
    discoveredSpreakerToken = null;
    document.getElementById("spToken").value = "";
    document.getElementById("spCode").value = "";
    // Spreaker-hanteringsfliken kan ha blivit tillgänglig nu.
    loadSpreakerStatus();
  } catch {
    setupStatus("Nätverksfel när inställningarna skulle sparas.", false);
  }
}

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

function updateQueueClearButtonCounts(items) {
  const doneCount = items.filter((it) => it.status === "done").length;
  const errorCount = items.filter((it) => it.status === "error" || it.status === "cancelled").length;
  const allCount = items.filter((it) => it.status !== "running").length;

  const doneBtn = document.getElementById("queueClearDoneBtn");
  doneBtn.textContent = `✅ Rensa klara (${doneCount} st)`;
  doneBtn.disabled = doneCount === 0;

  const errorsBtn = document.getElementById("queueClearErrorsBtn");
  errorsBtn.textContent = `🧹 Rensa fel/avbrutna (${errorCount} st)`;
  errorsBtn.disabled = errorCount === 0;

  const allBtn = document.getElementById("queueClearAllBtn");
  allBtn.textContent = `🗑️ Rensa allt (${allCount} st)`;
  allBtn.disabled = allCount === 0;
}

function renderQueueList(items) {
  updateQueueClearButtonCounts(items);

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
