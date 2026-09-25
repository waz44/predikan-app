// ---------------------------------------------------------------------------
// Globalt state
// ---------------------------------------------------------------------------
// Hela sidan är ett enda skript utan byggsteg eller ramverk: vanlig JavaScript
// som körs direkt i webbläsaren. Skriptet laddas sist i index.html, så alla
// element som hämtas med getElementById nedan finns redan när det körs.
//
// All kommunikation med servern sker via fetch() mot /api/... (se routers/).
//
// wavesurfer: vågformsspelaren för den uppladdade filen (null tills en fil laddats upp).
let wavesurfer = null;
// Wavesurfers tillägg för markerade områden - används för klippmarkeringen.
let regionsPlugin = null;
// Den blå markeringen i vågformen (start/slut för det som ska publiceras).
let activeRegion = null;
// Id för den uppladdade filen, från servern (se POST /api/upload).
let currentFileId = null;
// Hela filens längd i sekunder - används som slutpunkt om fältet är tomt.
let audioDuration = 0;
// Senast hämtade statistik, för tidsuppskattningen under vågformen.
let latestStats = null;

// Element som används på många ställen hämtas en gång här.
const uploadStatus = document.getElementById("uploadStatus");
const stepTrim = document.getElementById("step-trim");
const stepMetadata = document.getElementById("step-metadata");

// ---------------------------------------------------------------------------
// Mörkt/ljust läge
// Systemets/webbläsarens inställning (prefers-color-scheme) styr som
// standard (ren CSS, se style.css) - knappen låter användaren uttryckligen
// välja ett läge istället, sparat i localStorage så det kommer ihåg sig.
// ---------------------------------------------------------------------------
// Nyckeln i localStorage där det valda läget sparas. Samma nyckel läses av
// det lilla skriptet i index.html:s <head>, så att rätt läge sätts innan
// sidan hinner visas (annars blinkar den till i fel färger).
const THEME_STORAGE_KEY = "predikan-theme";

/**
 * Om mörkt läge gäller just nu.
 *
 * Ett uttryckligt val (data-theme på <html>) går före systemets inställning.
 * @returns {boolean} true om sidan visas i mörkt läge.
 */
function isDarkThemeActive() {
  // "dark", "light" eller null (inget val gjort - följ systemet).
  const explicit = document.documentElement.getAttribute("data-theme");
  // Uttryckligt val från knappen går alltid först.
  if (explicit === "dark") return true;
  if (explicit === "light") return false;
  // Inget eget val: fråga webbläsaren om systemet är inställt på mörkt läge.
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/**
 * Visar solen i mörkt läge (klicka för ljust) och månen i ljust läge.
 */
function updateThemeToggleButton() {
  const btn = document.getElementById("themeToggleBtn");
  // Skyddar om knappen skulle saknas i HTML (t.ex. i en äldre sidversion).
  if (!btn) return;
  btn.textContent = isDarkThemeActive() ? "☀️" : "🌙";
}

// Knappen uppe till höger: växla till motsatt läge och kom ihåg valet.
document.getElementById("themeToggleBtn").addEventListener("click", () => {
  // Växla till motsatsen av det som visas just nu - oavsett om det kom från
  // ett tidigare val eller från systemets inställning.
  const next = isDarkThemeActive() ? "light" : "dark";
  // Attributet på <html> styr vilka färgvariabler i style.css som gäller.
  document.documentElement.setAttribute("data-theme", next);
  try {
    // Sparas i webbläsaren (inte på servern), så valet gäller per dator.
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

// Rätt ikon på knappen direkt när sidan laddas.
updateThemeToggleButton();

// ---------------------------------------------------------------------------
// Flikar (Bearbeta predikningar / Hantera Spreaker)
// ---------------------------------------------------------------------------
// Varje flikknapp har data-tab="<id för fliken>" - ett klick visar den fliken.
document.querySelectorAll(".tab-btn").forEach((btn) => {
  // dataset.tab läser attributet data-tab från knappen.
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

/**
 * Visar en flik och döljer de andra.
 * @param {string} tabId "tab-process", "tab-spreaker" eller "tab-setup".
 */
function showTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    // Markera den valda flikknappen (understruken i style.css).
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  // toggle("hidden", villkor) lägger till klassen när villkoret är sant och
  // tar bort den annars - så exakt en flik blir synlig.
  document.getElementById("tab-process").classList.toggle("hidden", tabId !== "tab-process");
  document.getElementById("tab-spreaker").classList.toggle("hidden", tabId !== "tab-spreaker");
  document.getElementById("tab-setup").classList.toggle("hidden", tabId !== "tab-setup");
  // Bearbetningskön/statistiken hör bara hemma på den första fliken - på
  // Hantera Spreaker- och Inställningar-flikarna får huvudkolumnen hela bredden istället.
  document.querySelector(".queue-sidebar").classList.toggle("hidden", tabId !== "tab-process");
  // Inställningarna läses in på nytt varje gång fliken öppnas, så den
  // alltid visar det som faktiskt står i .env.
  if (tabId === "tab-setup") loadSetupConfig();
}

// ---------------------------------------------------------------------------
// Prestandastatistik & tidsuppskattning
// ---------------------------------------------------------------------------
/**
 * Hämtar statistiken (antal predikningar, total tid m.m.) och visar den.
 * Körs när sidan laddas; kvoten används sedan för tidsuppskattningen.
 */
async function loadStats() {
  try {
    // GET /api/stats svarar med t.ex.
    //   { total_count: 12, total_sermon_seconds: 28800,
    //     total_processing_seconds: 12960, processing_ratio: 0.45 }
    // processing_ratio är null innan första predikan bearbetats.
    // "if (!res.ok) return" nedan: ett fel lämnar rutan som den var.
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    // Spara statistiken - updateEtaHint behöver den varje gång klippet ändras.
    latestStats = await res.json();
    renderStatsSummary(latestStats);
    updateEtaHint();
  } catch {
    // Statistik är en extra funktion - fel här ska aldrig blockera resten av appen.
  }
}

/**
 * Skriver ut statistiken i rutan "Prestandastatistik".
 * @param {object} data Svaret från GET /api/stats.
 */
function renderStatsSummary(data) {
  const el = document.getElementById("statsSummary");
  // Ingen historik än: visa en förklaring i stället för en rad nollor.
  if (!data || !data.total_count) {
    el.textContent = "Ingen bearbetning genomförd ännu.";
    return;
  }
  // Kvoten = bearbetningssekunder per sekund predikan, t.ex. 0.45x.
  const ratioText = data.processing_ratio
    ? `${data.processing_ratio.toFixed(2)}x (bearbetningstid per sekund predikan)`
    : "-";
  // Fyra rader i rutan. Siffrorna kommer från servern (inte från användare),
  // så de behöver ingen escaping.
  el.innerHTML = `
    <p><strong>${data.total_count}</strong> predikningar bearbetade</p>
    <p>Total predikantid: <strong>${formatDuration(data.total_sermon_seconds)}</strong></p>
    <p>Total bearbetningstid: <strong>${formatDuration(data.total_processing_seconds)}</strong></p>
    <p>Snitthastighet: <strong>${ratioText}</strong></p>
  `;
}

/**
 * Uppskattar hur lång tid det valda klippet tar att bearbeta, utifrån
 * hittillsvarande snitt, och visar det under vågformen. Anropas varje gång
 * start- eller slutpunkten ändras.
 */
function updateEtaHint() {
  // Texten under vågformen.
  const hint = document.getElementById("etaHint");
  if (!hint) return;
  // Ingen historik ännu - hellre ingen uppskattning än en påhittad.
  if (!latestStats || !latestStats.processing_ratio) {
    hint.textContent = "";
    return;
  }
  // Tomma eller ogiltiga fält: start 0 och slut = hela filens längd.
  const start = parseFloat(document.getElementById("startInput").value) || 0;
  const end = parseFloat(document.getElementById("endInput").value) || audioDuration;
  // Klippets längd (aldrig negativ, även om slut < start i fälten), gånger
  // den historiska kvoten = uppskattad bearbetningstid. Exempel: 40 minuter
  // (2400 s) * 0.45 = 1080 s, som visas som "18min 0s".
  const clipSeconds = Math.max(0, end - start);
  const estimateSeconds = clipSeconds * latestStats.processing_ratio;
  hint.textContent = `⏱️ Uppskattad bearbetningstid för valt klipp: ~${formatDuration(estimateSeconds)} (baserat på snittet av ${latestStats.total_count} tidigare predikningar).`;
}

/**
 * Sekunder som kort läsbar tid: "1h 5min", "12min 4s" eller "45s".
 * @param {number} totalSeconds
 * @returns {string}
 */
function formatDuration(totalSeconds) {
  // Avrunda till hela sekunder; saknat värde (undefined/null) räknas som 0.
  const seconds = Math.round(totalSeconds || 0);
  // Dela upp i timmar, minuter och sekunder.
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  // Över en timme visas inga sekunder - de säger inget i det sammanhanget.
  if (h > 0) return `${h}h ${m}min`;
  if (m > 0) return `${m}min ${s}s`;
  return `${s}s`;
}

// Hämta statistiken direkt när sidan laddas.
loadStats();

// ---------------------------------------------------------------------------
// STEG 1: Uppladdning
// ---------------------------------------------------------------------------
// När en fil väljs laddas den upp direkt. Svaret ger ett id och filens
// längd; sedan ritas vågformen och steg 2-3 visas.
document.getElementById("fileInput").addEventListener("change", async (e) => {
  // Bara en fil i taget (filväljaren tillåter inte flera).
  const file = e.target.files[0];
  // Användaren stängde filväljaren utan att välja något.
  if (!file) return;

  // Visa att något händer - en stor fil kan ta en stund att ladda upp.
  uploadStatus.textContent = "Laddar upp...";
  uploadStatus.className = "status";

  // FormData skickar filen som en vanlig formuläruppladdning (multipart).
  const formData = new FormData();
  // Fältnamnet "file" måste matcha parametern i routers/upload.py.
  formData.append("file", file);

  try {
    // POST /api/upload svarar med
    //   { file_id: "...", filename: "predikan.mp3", duration_seconds: 2712.4 }
    // Vid fel (fel filtyp, för stor fil, oläsbart ljud) är res.ok false och
    // svaret innehåller "detail" med en förklaring på svenska.
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json();
      // Serverns felmeddelande (t.ex. fel filtyp eller för stor fil) visas.
      throw new Error(err.detail || "Uppladdning misslyckades");
    }
    const data = await res.json();
    // Id:t används sedan både för att spela upp filen och för att köa den.
    currentFileId = data.file_id;
    // Längden används som slutpunkt och för tidsuppskattningen.
    audioDuration = data.duration_seconds;

    uploadStatus.textContent = `✅ "${data.filename}" uppladdad (${formatTime(audioDuration)})`;
    uploadStatus.className = "status success";

    // Vänta på att vågformen skapats innan klipp- och metadatastegen visas.
    await initWaveform(currentFileId);
    // Visa steg 2 (klippning) och 3 (metadata) nu när en fil finns.
    stepTrim.classList.remove("hidden");
    stepMetadata.classList.remove("hidden");
  } catch (err) {
    // Felet visas i statusraden; inget annat på sidan ändras.
    uploadStatus.textContent = `❌ ${err.message}`;
    uploadStatus.className = "status error";
  }
});

// ---------------------------------------------------------------------------
// STEG 2: Vågform & klippning (Wavesurfer.js + Regions-plugin)
// ---------------------------------------------------------------------------
/**
 * Skapar vågformsspelaren för en uppladdad fil, med en klippmarkering som
 * från början täcker hela filen. Anropas också när läget (ljust/mörkt)
 * byts, eftersom vågformens färger bara kan sättas när den skapas.
 * @param {string} fileId Id för filen (se POST /api/upload).
 */
async function initWaveform(fileId) {
  if (wavesurfer) {
    // En tidigare vågform tas bort först, annars ritas två på varandra.
    wavesurfer.destroy();
  }

  // Tillägget för markeringar måste skapas på nytt för varje ny spelare.
  regionsPlugin = WaveSurfer.Regions.create();

  // Samma färger som sidans tema (se --primary m.fl. i style.css).
  const dark = isDarkThemeActive();
  // WaveSurfer laddas från unpkg.com i index.html (version 7). Inställningarna:
  // - container: elementet där vågformen ritas
  // - waveColor/progressColor: färg för ospelat/spelat ljud
  // - cursorColor: den lodräta linjen vid uppspelningspositionen
  // - height: vågformens höjd i pixlar
  // - url: varifrån ljudet hämtas
  // - plugins: tilläggen - här bara markeringarna (Regions)
  wavesurfer = WaveSurfer.create({
    container: "#waveform",
    waveColor: dark ? "#4a4d68" : "#c9c9ec",
    progressColor: dark ? "#8b7dff" : "#4a3aff",
    cursorColor: dark ? "#e7e8f0" : "#23243a",
    // Pixlar.
    height: 100,
    // Ljudet hämtas från servern (se GET /api/audio/{file_id}).
    url: `/api/audio/${fileId}`,
    // Utan tillägget går det inte att markera start och slut.
    plugins: [regionsPlugin],
  });

  // "ready": ljudet är inläst och vågformen ritad - då är längden känd.
  wavesurfer.on("ready", () => {
    // Filens längd i sekunder, enligt spelaren.
    const duration = wavesurfer.getDuration();
    // Skapa en förvald region som täcker hela klippet - användaren drar i kanterna
    // drag: markeringen kan flyttas i sidled. resize: kanterna kan dras.
    // Färgen är en genomskinlig variant av sidans lila huvudfärg.
    activeRegion = regionsPlugin.addRegion({
      start: 0,
      end: duration,
      color: "rgba(74, 58, 255, 0.15)",
      drag: true,
      resize: true,
    });
    // Fälten fylls med hela filen - användaren justerar sedan.
    document.getElementById("startInput").value = 0;
    // Tider visas med en decimal (tiondels sekund) - tillräckligt exakt för att
    // klippa mellan ord, utan att fälten blir svårlästa.
    document.getElementById("endInput").value = duration.toFixed(1);
    updateEtaHint();
  });

  // Uppdatera tidsvisningen medan ljudet spelas...
  wavesurfer.on("audioprocess", () => {
    document.getElementById("currentTime").textContent = formatTime(wavesurfer.getCurrentTime());
  });

  // ...och när användaren klickar någonstans i vågformen.
  wavesurfer.on("interaction", () => {
    document.getElementById("currentTime").textContent = formatTime(wavesurfer.getCurrentTime());
  });

  // När markeringen dras eller ändras i storlek: fyll i start/slut i fälten.
  regionsPlugin.on("region-updated", (region) => {
    // Kom ihåg markeringen, så att fälten kan flytta den senare.
    activeRegion = region;
    document.getElementById("startInput").value = region.start.toFixed(1);
    document.getElementById("endInput").value = region.end.toFixed(1);
    updateEtaHint();
  });
}

// Spelarknapparna. "if (wavesurfer)" skyddar mot klick innan en fil laddats upp.
document.getElementById("playBtn").addEventListener("click", () => {
  // Spela från nuvarande position.
  if (wavesurfer) wavesurfer.play();
});

document.getElementById("stopBtn").addEventListener("click", () => {
  // Stoppa och gå tillbaka till början.
  if (wavesurfer) wavesurfer.stop();
});

// Hoppa 15 sekunder bakåt/framåt - praktiskt för att hitta exakt var predikan börjar.
document.getElementById("backBtn").addEventListener("click", () => {
  // Negativa sekunder = bakåt.
  if (wavesurfer) wavesurfer.skip(-15);
});

document.getElementById("forwardBtn").addEventListener("click", () => {
  if (wavesurfer) wavesurfer.skip(15);
});

// "Sätt start/slut = nuvarande tid": använd spelarens position som klippunkt.
document.getElementById("setStartBtn").addEventListener("click", () => {
  if (!wavesurfer) return;
  // Var uppspelningen står just nu, i sekunder.
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

// Skriver man in tider för hand flyttas markeringen i vågformen efter.
document.getElementById("startInput").addEventListener("change", updateRegionFromInputs);
document.getElementById("endInput").addEventListener("change", updateRegionFromInputs);

/**
 * Flyttar markeringen i vågformen till tiderna i start- och slutfälten.
 */
function updateRegionFromInputs() {
  // Innan vågformen är klar finns ingen markering att flytta.
  if (activeRegion) {
    const start = parseFloat(document.getElementById("startInput").value) || 0;
    const end = parseFloat(document.getElementById("endInput").value) || audioDuration;
    // Flytta markeringen - det utlöser "region-updated", som i sin tur skriver
    // tillbaka samma värden i fälten (ofarligt, värdena är redan desamma).
    activeRegion.setOptions({ start, end });
  }
  updateEtaHint();
}

/**
 * Sekunder som mm:ss, t.ex. 125 -> "02:05".
 * @param {number} seconds
 * @returns {string}
 */
function formatTime(seconds) {
  // padStart(2, "0") ger alltid två siffror: 5 -> "05". Minuter över 99
  // visas med fler siffror, t.ex. "125:30".
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
// "➕ Lägg till i kö": skicka klipp och metadata till servern.
document.getElementById("metadataForm").addEventListener("submit", async (e) => {
  // Hindra webbläsarens vanliga formulärskick, som skulle ladda om sidan.
  e.preventDefault();

  // Formuläret visas bara efter en uppladdning, men dubbelkolla ändå.
  if (!currentFileId) {
    alert("Ladda upp en ljudfil först.");
    return;
  }

  // Samma fält som servern förväntar sig (se ProcessRequest i services/pipeline.py).
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

  // Talaren är det enda obligatoriska fältet (det skrivs alltid sist i
  // beskrivningen). Titel och beskrivning kan AI:n skriva.
  if (!payload.speaker) {
    alert("Talare är ett obligatoriskt fält.");
    return;
  }

  const addBtn = document.getElementById("processBtn");
  // Knappen spärras medan anropet pågår, så ett dubbelklick inte köar två gånger.
  addBtn.disabled = true;

  try {
    // POST /api/process med formulärets värden som JSON. Svaret är
    //   { job_id: "...", queue_id: "..." }
    // Jobbet körs sedan i bakgrunden - framstegen syns i kön till höger.
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Kunde inte lägga till i kön");
    }

    // Bekräftelsen visas i uppladdningsrutan, som är det enda steget som
    // syns efter återställningen nedan.
    uploadStatus.textContent = `✅ "${payload.title || payload.speaker}" tillagd i bearbetningskön.`;
    uploadStatus.className = "status success";

    // Återställ flödet så nästa fil kan laddas upp direkt
    // Allt återställs: fil-id, längd, filväljare, formulär, steg 2-3 döljs
    // och vågformen tas bort. Då är sidan redo för nästa predikan direkt,
    // medan den förra bearbetas i kön.
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

    // Visa det nya objektet i kön direkt, utan att vänta på nästa uppdatering.
    loadQueue();
  } catch (err) {
    // Felet visas i en dialogruta; formuläret lämnas ifyllt så inget behöver skrivas om.
    alert(err.message);
  } finally {
    addBtn.disabled = false;
  }
});

/**
 * Gör text säker att stoppa in i HTML: <, > och & blir ofarliga.
 *
 * Texten sätts som textContent på ett tillfälligt element och läses sedan
 * tillbaka som HTML - webbläsaren sköter själv all escaping. Används för
 * allt som kommer från användare, AI eller Spreaker innan det visas.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
  // Elementet läggs aldrig in på sidan - det används bara som "översättare".
  const div = document.createElement("div");
  // null och undefined blir tom text i stället för ordet "null".
  div.textContent = str || "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Bulk-import via CSV
// Raderna läggs till i samma bearbetningskö som manuellt klippta filer (se
// nedan) - status/framsteg för dem visas enbart i kösektionen.
// ---------------------------------------------------------------------------
// "📥 Importera CSV": skicka CSV-filen till servern, som kontrollerar hela
// filen och köar raderna (se routers/bulk_import.py).
document.getElementById("bulkImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("bulkCsvInput");
  const file = fileInput.files[0];
  // Statusraden under importknappen.
  const bulkStatus = document.getElementById("bulkImportStatus");

  if (!file) {
    bulkStatus.textContent = "Välj en CSV-fil först.";
    bulkStatus.className = "status error";
    return;
  }

  // Servern kontrollerar hela filen innan något köas - det går snabbt.
  bulkStatus.textContent = "Läser in och validerar CSV-filen...";
  bulkStatus.className = "status";
  // Knappen spärras tills svaret kommit (återställs i finally nedan).
  document.getElementById("bulkImportBtn").disabled = true;

  const formData = new FormData();
  formData.append("file", file);

  try {
    // Svaret läses som JSON både vid lyckat och misslyckat anrop - vid fel
    // innehåller det en beskrivning av alla felaktiga rader.
    const res = await fetch("/api/bulk-import", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Import misslyckades");
    }

    // data.items: en post per köad rad, i CSV-filens ordning.
    bulkStatus.textContent = `✅ ${data.items.length} predikning(ar) tillagda i bearbetningskön (se kön till höger).`;
    bulkStatus.className = "status success";
    // Töm filväljaren, så samma fil inte importeras två gånger av misstag.
    fileInput.value = "";
    loadQueue();
  } catch (err) {
    // Vid fel i filen listar meddelandet alla felaktiga rader (en per rad).
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
// Alla avsnitt i listan, i aktuell sorteringsordning. Varje avsnitt är ett
// objekt från servern (episode_id, title, description ...) som dessutom får
// saved_title/saved_description/suggested (se rememberSavedValues).
let spreakerEpisodes = [];
// Aktuell sortering: kolumn och riktning ("asc" = stigande, "desc" = fallande).
// Standard: senast publicerade först.
let spreakerSort = { field: "published_at", dir: "desc" };
// Id:n för avsnitt med osparade ändringar - styr gul markering och Spara-knappar.
const spreakerDirty = new Set();
// Kolumner som ska sorteras som tal (och true/false), inte alfabetiskt.
const SPREAKER_NUMERIC_FIELDS = new Set(["duration_seconds", "plays_count", "archived"]);

// Paginering sker helt i minnet (hela listan finns redan i spreakerEpisodes)
// - sortering gäller alltid HELA listan, sen visas bara aktuell sida.
// Sidstorleken kommer ihåg sig per webbläsare; 0 = visa alla.
const SPREAKER_PAGE_SIZE_KEY = "predikan-spreaker-page-size";
// Aktuell sida i listan (1 = första).
let spreakerPage = 1;
// Avsnitt per sida; 0 = alla. Ersätts nedan av ett sparat val om det finns.
let spreakerPageSize = 25;
try {
  // Ett tidigare val i den här webbläsaren. NaN (inget sparat) ignoreras.
  const saved = parseInt(localStorage.getItem(SPREAKER_PAGE_SIZE_KEY), 10);
  if (!Number.isNaN(saved)) spreakerPageSize = saved;
} catch {
  // localStorage kan vara blockerat - standardvärdet gäller då.
}
// Visa den sparade sidstorleken i listrutan.
document.getElementById("spreakerPageSize").value = String(spreakerPageSize);

/**
 * Talaren ur beskrivningens sista rad "Talare: X" (samma regel som servern).
 * @param {string} description
 * @returns {string|null} Namnet, eller null om ingen sådan rad finns.
 */
function extractSpeaker(description) {
  // Tom beskrivning - ingen talare.
  if (!description) return null;
  // g = alla träffar, i = oavsett versaler, m = ^ och $ gäller varje rad.
  const matches = [...description.matchAll(/^Talare:\s*(.+)$/gim)];
  if (!matches.length) return null;
  // Sista matchningen gäller (om beskrivningen redigerats och fått två rader).
  // [1] är det som fångades av parentesen i uttrycket, alltså namnet.
  const last = matches[matches.length - 1][1].trim();
  return last || null;
}

/**
 * Frågar servern vad som är konfigurerat och visar Spreaker-fliken och dess
 * rutor därefter. Anropas när sidan laddas och efter att inställningar sparats.
 */
async function loadSpreakerStatus() {
  try {
    // GET /api/spreaker/status svarar med
    //   { configured: true/false, archive_available: true/false }
    // - configured: token och show-id finns och simulering är av -> avsnittslistan
    // - archive_available: show-id finns -> podd-arkivet (kräver ingen token)
    const res = await fetch("/api/spreaker/status");
    if (!res.ok) return;
    const data = await res.json();
    // Fliken visas om minst en av rutorna (avsnittslistan eller arkivet) kan användas.
    if (data.configured || data.archive_available) {
      document.getElementById("spreakerTabBtn").classList.remove("hidden");
    }
    // Varje ruta visas bara om den kan användas.
    document.getElementById("spreakerManageCard").classList.toggle("hidden", !data.configured);
    document.getElementById("archiveCard").classList.toggle("hidden", !data.archive_available);
    // Fyll avsnittslistan och arkivrutan direkt, så fliken är klar när den öppnas.
    if (data.configured) loadSpreakerEpisodes();
    if (data.archive_available) loadArchiveStatus();
  } catch {
    // Spreaker-hantering är en extra funktion - fel här ska inte blockera resten av appen.
  }
}

/**
 * Hämtar den lokalt sparade avsnittslistan (inget anrop mot Spreaker) och
 * ritar tabellen. Osparade ändringar i listan skrivs över.
 */
async function loadSpreakerEpisodes() {
  try {
    // GET /api/spreaker/episodes: den lokalt sparade listan - snabb, inget
    // anrop mot Spreaker. Svaret är { items: [ ...avsnitt ] } där varje
    // avsnitt har episode_id, title, description, speaker, published_at,
    // duration_seconds, plays_count, site_url, archived och has_transcript.
    const res = await fetch("/api/spreaker/episodes");
    if (!res.ok) return;
    const data = await res.json();
    spreakerEpisodes = data.items || [];
    // Kom ihåg varje avsnitts sparade titel/beskrivning, för jämförelser.
    spreakerEpisodes.forEach(rememberSavedValues);
    // En nyinläst lista har inga osparade ändringar.
    spreakerDirty.clear();
    sortSpreakerEpisodes();
    renderSpreakerTable();
  } catch {
    // Tyst - "Hämta från Spreaker"-knappen visar fel explicit vid ett faktiskt hämtningsförsök.
  }
}

/**
 * Sorterar spreakerEpisodes på plats enligt spreakerSort. Sker helt i
 * minnet - inga nya anrop till servern.
 */
function sortSpreakerEpisodes() {
  const { field, dir } = spreakerSort;
  // Beskrivningskolumnen går inte att sortera på.
  if (field === "none") return;
  // Multiplicera jämförelsen med -1 för att vända ordningen.
  const mult = dir === "asc" ? 1 : -1;
  // sort() anropar funktionen med två avsnitt i taget. Den ska returnera
  // ett negativt tal om a ska stå först, ett positivt om b ska stå först
  // och 0 om de är lika.
  spreakerEpisodes.sort((a, b) => {
    let av;
    let bv;
    // Talaren sorteras på det som står i beskrivningen just nu (även osparat).
    if (field === "speaker") {
      av = extractSpeaker(a.description) || "";
      bv = extractSpeaker(b.description) || "";
    } else {
      // Övriga kolumner: sortera direkt på fältets värde.
      av = a[field];
      bv = b[field];
    }
    // Tal: saknade värden (?? = null/undefined) hamnar sist vid fallande sortering.
    if (SPREAKER_NUMERIC_FIELDS.has(field)) {
      av = av ?? -Infinity;
      bv = bv ?? -Infinity;
      return (av - bv) * mult;
    }
    // Text: jämför utan hänsyn till versaler.
    av = (av || "").toString().toLowerCase();
    bv = (bv || "").toString().toLowerCase();
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return 0;
  });
}

/**
 * Uppdaterar sidvalet under tabellen och räknar ut vilka avsnitt som hör
 * till aktuell sida.
 * @returns {object[]} Avsnitten som ska visas på aktuell sida.
 */
function renderSpreakerPaginator() {
  // Antal avsnitt totalt, över alla sidor.
  const total = spreakerEpisodes.length;
  // Sidstorlek 0 = "Alla" - då finns bara en sida.
  const pageCount = spreakerPageSize ? Math.max(1, Math.ceil(total / spreakerPageSize)) : 1;
  // Håll sidnumret inom 1..antal sidor (listan kan ha krympt).
  spreakerPage = Math.min(Math.max(1, spreakerPage), pageCount);
  // Index för första och (ett efter) sista avsnittet på sidan.
  const first = spreakerPageSize ? (spreakerPage - 1) * spreakerPageSize : 0;
  const last = spreakerPageSize ? Math.min(total, first + spreakerPageSize) : total;

  // Sidvalet döljs när listan är tom.
  document.getElementById("spreakerPaginator").classList.toggle("hidden", !total);
  // T.ex. "Sida 2 av 4 · visar 26–50 av 83". En tom lista visar "0–0 av 0".
  document.getElementById("spreakerPageInfo").textContent =
    `Sida ${spreakerPage} av ${pageCount} · visar ${total ? first + 1 : 0}–${last} av ${total}`;
  // Föregående/Nästa spärras på första respektive sista sidan.
  document.getElementById("spreakerPrevBtn").disabled = spreakerPage <= 1;
  document.getElementById("spreakerNextBtn").disabled = spreakerPage >= pageCount;
  // slice tar med first men inte last.
  return spreakerEpisodes.slice(first, last);
}

// Kommer ihåg vad som faktiskt ligger sparat på Spreaker (saved_title/
// saved_description), så ett AI-förslag kan visas bredvid den nuvarande
// versionen och ångras. ep.suggested = { title, description } markerar
// vilka fält som just nu innehåller ett ogranskat AI-förslag.
/**
 * @param {object} ep Ett avsnitt i listan (ändras på plats).
 */
function rememberSavedValues(ep) {
  // saved_* = det som ligger på Spreaker. title/description = det som visas
  // (och kanske redigerats). suggested = vilka fält som har ett AI-förslag.
  ep.saved_title = ep.title || "";
  ep.saved_description = ep.description || "";
  // Inga ogranskade förslag direkt efter inläsning eller sparning.
  ep.suggested = {};
}

/**
 * Om avsnittets titel och beskrivning är desamma som det som är sparat.
 * @param {object} ep
 * @returns {boolean}
 */
function isEpisodeUnchanged(ep) {
  return (ep.title || "") === ep.saved_title && (ep.description || "") === ep.saved_description;
}

// Visar den nuvarande (sparade) versionen bredvid ett AI-förslag.
/**
 * @param {object} ep Avsnittet.
 * @param {"title"|"description"} field Vilket fält.
 * @returns {string} HTML för rutan "Nuvarande", eller "" om ingen jämförelse ska visas.
 */
function renderSuggestionCompare(ep, field) {
  // Det som ligger på Spreaker respektive det som visas i fältet just nu.
  const saved = field === "title" ? ep.saved_title : ep.saved_description;
  const current = field === "title" ? ep.title : ep.description;
  // Visa bara jämförelsen när fältet har ett ogranskat AI-förslag som skiljer sig.
  if (!ep.suggested || !ep.suggested[field] || saved === (current || "")) return "";
  // Rutan "Nuvarande": den sparade texten (skrivskyddad) och en knapp för
  // att behålla den. data-field talar om för klickhanteraren vilket fält
  // knappen gäller.
  return `
    <div class="spreaker-compare-current">
      <div class="spreaker-compare-label">Nuvarande</div>
      <div class="spreaker-compare-text">${escapeHtml(saved) || "<em>(tom)</em>"}</div>
      <button type="button" class="spreaker-revert-btn" data-field="${field}">↩️ Behåll nuvarande</button>
    </div>`;
}

/**
 * Lägger redigeringsfältet bredvid rutan "Nuvarande" när det finns ett
 * AI-förslag - annars returneras fältet som det är.
 * @param {object} ep Avsnittet.
 * @param {"title"|"description"} field Vilket fält.
 * @param {string} inputHtml HTML för själva redigeringsfältet.
 * @returns {string} HTML för cellens innehåll.
 */
function renderSuggestionField(ep, field, inputHtml) {
  // Utan förslag: bara fältet. Med förslag: två kolumner sida vid sida
  // (.spreaker-compare är ett rutnät i style.css) - nuvarande till vänster,
  // förslaget (redigerbart) till höger.
  const compare = renderSuggestionCompare(ep, field);
  // Inget förslag att jämföra med - bara fältet, som vanligt.
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

/**
 * Sparar ett avsnitts titel och beskrivning till Spreaker. Lyckas det blir
 * det sparade det nya "nuvarande", och raden räknas inte längre som ändrad.
 * @param {object} ep Avsnittet.
 * @throws {Error} Med serverns felmeddelande om sparningen misslyckas.
 */
async function saveSpreakerEpisode(ep) {
  // PUT /api/spreaker/episodes/{id} med { title, description }. Servern skickar
  // ändringen till Spreaker och uppdaterar sin lokala lista. Svar: { updated: true },
  // eller ett fel med "detail" (t.ex. tom titel eller fel från Spreaker).
  const res = await fetch(`/api/spreaker/episodes/${ep.episode_id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: ep.title, description: ep.description }),
  });
  if (!res.ok) {
    // Ett felsvar som inte är JSON ger ett tomt objekt i stället för ett nytt fel.
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Kunde inte spara.");
  }
  // Det som nu är sparat blir det nya "nuvarande".
  rememberSavedValues(ep);
  spreakerDirty.delete(ep.episode_id);
}

/**
 * Ritar om hela tabellen (bara aktuell sida). Anropas efter varje ändring
 * som påverkar visningen: sortering, sidbyte, sparning, AI-förslag.
 *
 * All text från Spreaker eller AI går genom escapeHtml, så en titel med
 * t.ex. "<" aldrig kan tolkas som HTML.
 */
function renderSpreakerTable() {
  const body = document.getElementById("spreakerTableBody");
  // Sidvalet räknar ut vilka avsnitt som hör till aktuell sida.
  const pageItems = renderSpreakerPaginator();
  // "Spara ändringar" går bara att klicka på när något är ändrat.
  document.getElementById("spreakerSaveBtn").disabled = spreakerDirty.size === 0;
  // Tom lista: en rad som förklarar hur den fylls.
  if (!spreakerEpisodes.length) {
    body.innerHTML = `<tr><td colspan="8" class="queue-empty">Inget hämtat ännu - klicka "Hämta från Spreaker".</td></tr>`;
    return;
  }

  // En tabellrad (<tr>) per avsnitt på sidan, med kolumnerna:
  //   Titel (fält + 🤖-knapp + ev. "Transkribera om") | Talare | Publicerad |
  //   Längd | Avspelningar | Arkiv | Beskrivning (fält + 🤖-knapp) | Spara
  // data-episode-id på raden gör att klick- och inmatningshanterarna vet
  // vilket avsnitt det gäller. Statusrutorna (.spreaker-regen-status) fylls
  // medan "Generera om" pågår.
  body.innerHTML = pageItems
    .map((ep) => {
      // Spreaker anger UTC-tid som "2026-09-20 08:30:00" - gör om till ISO-format
      // med Z (= UTC) och visa som datum i svensk form.
      const publishedLabel = ep.published_at ? new Date(ep.published_at.replace(" ", "T") + "Z").toLocaleDateString("sv-SE") : "-";
      // Saknade värden visas som "-" i stället för tomt eller "null".
      const durationLabel = ep.duration_seconds ? formatDuration(ep.duration_seconds) : "-";
      const playsLabel = ep.plays_count != null ? ep.plays_count : "-";
      // Talaren räknas fram ur beskrivningen varje gång, så den alltid stämmer.
      const speaker = extractSpeaker(ep.description) || "-";
      // Ändrade rader får klassen "dirty" (markeras i style.css).
      const dirtyClass = spreakerDirty.has(ep.episode_id) ? " dirty" : "";
      // 🗄️ = ljudet finns i det lokala arkivet, 📝 = ett transkript finns sparat.
      // Tomma delar filtreras bort; inget alls visas som "-".
      const archiveLabel = [
        ep.archived ? `<span title="Ljudet finns i det lokala arkivet">🗄️</span>` : "",
        ep.has_transcript ? `<span title="Transkript finns sparat">📝</span>` : "",
      ].join(" ").trim() || "-";
      // "Transkribera om" är bara meningsfull när ett sparat transkript finns.
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

// Klick på en kolumnrubrik sorterar på den kolumnen; ett nytt klick vänder ordningen.
document.querySelectorAll("#spreakerTable th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    // data-sort på rubriken anger vilket fält kolumnen sorterar på.
    const field = th.dataset.sort;
    if (field === "none") return;
    // Samma kolumn igen: vänd riktningen. Ny kolumn: börja stigande.
    spreakerSort = spreakerSort.field === field
      ? { field, dir: spreakerSort.dir === "asc" ? "desc" : "asc" }
      : { field, dir: "asc" };

    // Pilen (▲/▼) visas bara på den kolumn som sorteras - ta bort den från
    // alla rubriker och sätt den på den klickade.
    document.querySelectorAll("#spreakerTable th[data-sort]").forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
    // sort-asc/sort-desc visar ▲ respektive ▼ (se style.css).
    th.classList.add(spreakerSort.dir === "asc" ? "sort-asc" : "sort-desc");

    sortSpreakerEpisodes();
    // Efter ny sortering visas första sidan.
    spreakerPage = 1;
    renderSpreakerTable();
  });
});

// Föregående/Nästa sida. renderSpreakerPaginator håller sidnumret inom gränserna.
document.getElementById("spreakerPrevBtn").addEventListener("click", () => {
  // Minskar sidnumret; renderSpreakerTable ritar om aktuell sida.
  spreakerPage -= 1;
  renderSpreakerTable();
});

document.getElementById("spreakerNextBtn").addEventListener("click", () => {
  spreakerPage += 1;
  renderSpreakerTable();
});

// Ny sidstorlek: börja om på sida 1 och kom ihåg valet i webbläsaren.
document.getElementById("spreakerPageSize").addEventListener("change", (e) => {
  // "0" (Alla) och ogiltiga värden ger 0 = visa hela listan på en sida.
  spreakerPageSize = parseInt(e.target.value, 10) || 0;
  spreakerPage = 1;
  try {
    // Kom ihåg sidstorleken i den här webbläsaren.
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
  // Händelsedelegering: en enda lyssnare på tabellen, och closest() hittar
  // raden som ändrades. Då behövs inga nya lyssnare när tabellen ritas om.
  const row = e.target.closest(".spreaker-row");
  if (!row) return;
  // data-episode-id är text i HTML - gör om till tal för att hitta avsnittet.
  const episodeId = parseInt(row.dataset.episodeId, 10);
  const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
  if (!ep) return;

  if (e.target.classList.contains("spreaker-title-input")) {
    // Ändringen sparas direkt i avsnittsobjektet, så den finns kvar vid sidbyte.
    ep.title = e.target.value;
  } else if (e.target.classList.contains("spreaker-description-input")) {
    // Beskrivningen ändrades - uppdatera även talarkolumnen på raden.
    ep.description = e.target.value;
    const speakerCell = row.querySelector(".spreaker-speaker-cell");
    // Talarkolumnen följer beskrivningens "Talare:"-rad medan man skriver.
    if (speakerCell) speakerCell.textContent = extractSpeaker(ep.description) || "-";
  }

  // Första ändringen på raden: markera den gul och lås upp dess Spara-knapp.
  // Raden ritas INTE om medan man skriver - då skulle markören hoppa.
  if (!spreakerDirty.has(episodeId)) {
    spreakerDirty.add(episodeId);
    row.classList.add("dirty");
  }
  // Radens Spara-knapp blir klickbar direkt vid första ändringen.
  row.querySelector(".spreaker-row-save-btn").disabled = false;
  document.getElementById("spreakerSaveBtn").disabled = spreakerDirty.size === 0;
});

// Per rad: "↩️ Behåll nuvarande" (ångra ett AI-förslag) och "💾 Spara".
document.getElementById("spreakerTableBody").addEventListener("click", async (e) => {
  // Klicket kan ha träffat en ikon inuti knappen - closest() hittar knappen.
  const revertBtn = e.target.closest(".spreaker-revert-btn");
  const saveBtn = e.target.closest(".spreaker-row-save-btn");
  // Andra klick i tabellen (t.ex. i ett textfält) hanteras inte här.
  if (!revertBtn && !saveBtn) return;
  const row = e.target.closest(".spreaker-row");
  const episodeId = parseInt(row.dataset.episodeId, 10);
  const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
  if (!ep) return;

  if (revertBtn) {
    const field = revertBtn.dataset.field;
    // Tillbaka till den sparade texten för just det fältet.
    ep[field] = field === "title" ? ep.saved_title : ep.saved_description;
    // Återställ fältet till det sparade och släng förslaget.
    ep.suggested[field] = false;
    // Är båda fälten nu som det sparade räknas raden inte längre som ändrad.
    if (isEpisodeUnchanged(ep)) spreakerDirty.delete(episodeId);
    renderSpreakerTable();
    return;
  }

  // Radens Spara: spärra knappen, visa "Sparar..." och skicka till Spreaker.
  // Vid fel visas felet på raden och knappen låses upp igen.
  const statusEl = row.querySelector(".spreaker-row-save-status");
  saveBtn.disabled = true;
  statusEl.className = "spreaker-row-save-status";
  statusEl.textContent = "Sparar...";
  try {
    // Kastar ett fel om Spreaker avvisar ändringen - då hoppar vi till catch.
    await saveSpreakerEpisode(ep);
    renderSpreakerTable();
    // Tabellen har ritats om - leta upp radens NYA statusruta för "✅ Sparad".
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
  // Bara klick på 🤖-knapparna hanteras här.
  const btn = e.target.closest(".spreaker-regen-btn");
  if (!btn) return;
  const row = btn.closest(".spreaker-row");
  const episodeId = parseInt(row.dataset.episodeId, 10);
  // Vilket av 🤖-knapparna (titel eller beskrivning) som klickades.
  const field = btn.dataset.field; // "title" | "description"
  const retranscribeCheckbox = row.querySelector(".spreaker-retranscribe-checkbox");
  // Kryssrutan finns bara när ett transkript är sparat - annars blir det
  // alltid en ny transkribering ändå.
  const forceRetranscribe = retranscribeCheckbox ? retranscribeCheckbox.checked : false;
  const statusEl = row.querySelector(`.spreaker-regen-status[data-status-for="${field}"]`);

  // Knappen spärras tills jobbet är klart (eller misslyckats).
  btn.disabled = true;
  if (statusEl) {
    statusEl.textContent = "⏳ Köar...";
    statusEl.className = "spreaker-regen-status";
  }

  try {
    // Köa ett "Generera om"-jobb för just det fält vars knapp klickades.
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
    // Jobbet är köat - följ det tills förslaget är klart.
    pollRegenerateJob(data.job_id, episodeId, statusEl, btn);
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = `❌ ${err.message}`;
      statusEl.className = "spreaker-regen-status error";
    }
    btn.disabled = false;
  }
});

/**
 * Frågar servern var 1,5 sekund hur ett "Generera om"-jobb går, tills det
 * är klart. Då fylls förslaget i avsnittet och visas bredvid nuvarande text.
 * @param {string} jobId Jobbets id.
 * @param {number} episodeId Avsnittet som jobbet gäller.
 * @param {HTMLElement|null} statusEl Rutan där procent/fel visas.
 * @param {HTMLButtonElement|null} btn Knappen, som låses upp igen vid fel.
 */
function pollRegenerateJob(jobId, episodeId, statusEl, btn) {
  const poll = async () => {
    let data;
    try {
      // Samma statusanrop som kön använder. Svaret har bl.a. status
      // (queued/running/done/error/cancelled), overall_percent och, när jobbet
      // är klart, result = { episode_id, title, description } där bara det
      // fält som genererades om har ett värde.
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

    // Väntar eller pågår: visa procenten och fråga igen om en stund.
    if (data.status === "running" || data.status === "queued") {
      if (statusEl) statusEl.textContent = `⏳ ${data.overall_percent || 0}%...`;
      // Fortfarande igång - fråga igen om 1,5 sekund.
      setTimeout(poll, 1500);
      return;
    }

    // Klart: lägg in förslaget i avsnittet, markera raden som ändrad och rita om.
    if (data.status === "done") {
      const ep = spreakerEpisodes.find((x) => x.episode_id === episodeId);
      if (ep) {
        // Markera fälten som AI-förslag, så de visas bredvid nuvarande version.
        // "!= null": bara de fält som faktiskt genererades om har ett värde.
        ep.suggested = ep.suggested || {};
        if (data.result.title != null) {
          ep.title = data.result.title;
          ep.suggested.title = true;
        }
        if (data.result.description != null) {
          ep.description = data.result.description;
          ep.suggested.description = true;
        }
        // Ett transkript finns nu sparat, så "Transkribera om" blir valbart.
        // Raden räknas som ändrad: förslaget är inte sparat på Spreaker än.
        ep.has_transcript = true;
        spreakerDirty.add(episodeId);
        document.getElementById("spreakerSaveBtn").disabled = false;
      }
      renderSpreakerTable();
      const globalStatus = document.getElementById("spreakerStatus");
      // Meddelandet visas överst, eftersom raden kan ligga på en annan sida.
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
  // Första frågan direkt, sedan var 1,5 sekund tills jobbet är klart.
  poll();
}

// "🔄 Hämta från Spreaker": hämta en färsk lista (kan ta en stund - servern
// gör ett anrop per avsnitt). Osparade ändringar i listan försvinner.
document.getElementById("spreakerFetchBtn").addEventListener("click", async () => {
  const btn = document.getElementById("spreakerFetchBtn");
  const status = document.getElementById("spreakerStatus");
  btn.disabled = true;
  // Knappen spärras och ett meddelande visas - hämtningen kan ta en stund.
  status.textContent = "Hämtar från Spreaker...";
  status.className = "status";
  try {
    // POST /api/spreaker/episodes/fetch: servern hämtar ALLA avsnitt från
    // Spreaker (ett anrop per avsnitt, så det kan ta en halv minut för ett
    // par hundra avsnitt) och ersätter sin lokala lista. Svaret har samma
    // form som GET /api/spreaker/episodes.
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
    // Allt klart: tala om hur många avsnitt kontot har.
    status.textContent = `✅ ${spreakerEpisodes.length} avsnitt hämtade.`;
    status.className = "status success";
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "status error";
  } finally {
    btn.disabled = false;
  }
});

// "💾 Spara ändringar": spara alla ändrade rader, en i taget. Misslyckas en
// rad fortsätter de andra, och den misslyckade ligger kvar som ändrad.
document.getElementById("spreakerSaveBtn").addEventListener("click", async () => {
  const btn = document.getElementById("spreakerSaveBtn");
  const status = document.getElementById("spreakerStatus");
  // Kopia av mängden - saveSpreakerEpisode tar bort id:n ur den under loopen.
  const idsToSave = Array.from(spreakerDirty);
  // Inget ändrat - knappen borde vara spärrad, men kontrollera ändå.
  if (!idsToSave.length) return;

  btn.disabled = true;
  status.textContent = `Sparar ${idsToSave.length} ändrade avsnitt...`;
  status.className = "status";

  let savedCount = 0;
  let failedCount = 0;
  // En i taget (await i loopen), inte alla samtidigt - Spreaker begränsar
  // hur många anrop som får göras per sekund.
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
  // Sammanfattning: hur många som sparades och hur många som misslyckades.
  status.textContent = failedCount
    ? `⚠️ ${savedCount} sparade, ${failedCount} misslyckades (försök igen).`
    : `✅ ${savedCount} avsnitt sparade.`;
  status.className = failedCount ? "status error" : "status success";
});

// Kontrollera direkt vid sidladdning om Spreaker-fliken ska visas.
loadSpreakerStatus();

// ---------------------------------------------------------------------------
// Lokalt podd-arkiv (modules/podcast_archive.py). Körs i en egen
// bakgrundstråd på servern - här pollas bara statusen medan den pågår.
// ---------------------------------------------------------------------------
// Timer för nästa statusfråga medan en arkivering pågår (så den kan stoppas).
let archivePollTimer = null;
// Om arkiveringen pågick vid förra frågan - för att märka när den just blev klar.
let archiveWasRunning = false;

// Hämtar om bara arkivinfon (🗄️/📝) för listan, utan att tappa osparade ändringar.
/**
 * Uppdaterar bara kolumnen Arkiv (🗄️/📝) i avsnittslistan, t.ex. efter
 * en arkivering. Titlar och beskrivningar lämnas orörda, så osparade
 * ändringar i listan finns kvar.
 */
async function refreshSpreakerArchiveInfo() {
  try {
    const res = await fetch("/api/spreaker/episodes");
    if (!res.ok) return;
    // En karta episode_id -> avsnitt gör varje uppslag nedan omedelbart.
    const fresh = new Map(((await res.json()).items || []).map((ep) => [ep.episode_id, ep]));
    for (const ep of spreakerEpisodes) {
      const f = fresh.get(ep.episode_id);
      if (f) {
        // Bara arkivfälten uppdateras - inte titel eller beskrivning.
        ep.archived = f.archived;
        ep.has_transcript = f.has_transcript;
      }
    }
    renderSpreakerTable();
  } catch {
    // Tyst - kolumnen uppdateras nästa gång listan laddas.
  }
}

/**
 * Byte som läsbar storlek, t.ex. "67.3 MB".
 * @param {number} bytes
 * @returns {string}
 */
function formatBytes(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} kB`;
}

/**
 * Visar arkiveringens status i rutan "Lokalt podd-arkiv": knapparnas
 * lägen, framstegsmätaren och en text om hur det går eller gick.
 * @param {object} s Svaret från GET /api/spreaker/archive/status.
 */
function renderArchiveStatus(s) {
  // Visa vilken mapp arkivet skrivs till (inställbar med ARCHIVE_DIR).
  document.getElementById("archiveDir").textContent = s.archive_dir || "-";
  // Starta går inte att klicka på medan en körning pågår; Avbryt visas bara då.
  document.getElementById("archiveRunBtn").disabled = s.running;
  document.getElementById("archiveStopBtn").classList.toggle("hidden", !s.running);
  document.getElementById("archiveStopBtn").disabled = s.stopping;
  const track = document.getElementById("archiveProgressTrack");
  const fill = document.getElementById("archiveProgressFill");
  const status = document.getElementById("archiveStatus");
  // Mätaren visas bara medan en körning pågår.
  track.classList.toggle("hidden", !s.running);
  status.className = "status";

  if (s.running) {
    // Totalt framsteg: färdiga avsnitt plus andelen av det som laddas ner just
    // nu, delat med antalet avsnitt. Ex: avsnitt 3 av 10, halvvägs = 25 %.
    const pct = s.total ? ((s.index - 1 + (s.bytes_total ? s.bytes_done / s.bytes_total : 0)) / s.total) * 100 : 0;
    // Mätarens fyllning, begränsad till 0-100 %.
    fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    // T.ex. " - 27.8 MB av 67.3 MB" medan en fil laddas ner.
    const bytes = s.bytes_done ? ` - ${formatBytes(s.bytes_done)}${s.bytes_total ? " av " + formatBytes(s.bytes_total) : ""}` : "";
    status.textContent = s.total
      ? `${s.stopping ? "Avbryter... " : ""}[${s.index}/${s.total}] ${s.current}${bytes}`
      : s.current;
    return;
  }
  // Ingen körning har gjorts sedan servern startade - visa ingenting.
  if (!s.finished_at) {
    status.textContent = "";
    return;
  }
  // Hela körningen stoppades (t.ex. arkivdisken saknas eller flödet gick inte att hämta).
  if (s.error) {
    status.className = "status error";
    status.textContent = `❌ ${s.error}`;
    return;
  }
  // Körningen är klar: sammanfatta resultatet och lista eventuella fel.
  const dl = s.downloaded ? ` (${formatBytes(s.downloaded_bytes)})` : "";
  let text = `Nedladdade: ${s.downloaded}${dl} · Fanns redan: ${s.skipped} · Misslyckade: ${s.failures.length}`;
  if (s.failures.length) text += "\n" + s.failures.map((f) => `- ${f}`).join("\n");
  status.className = s.failures.length ? "status error" : "status success";
  // pre-line: radbrytningarna i texten visas (en rad per misslyckat avsnitt).
  status.style.whiteSpace = "pre-line";
  status.textContent = (s.failures.length ? "⚠️ " : "✅ ") + text;
}

/**
 * Hämtar och visar arkiveringens status. Pågår en körning frågar den igen
 * varje sekund; när den just blivit klar uppdateras avsnittslistans
 * Arkiv-kolumn.
 */
async function loadArchiveStatus() {
  try {
    // GET /api/spreaker/archive/status: running, stopping, total, index,
    // current (titel), bytes_done/bytes_total (aktuell fil), downloaded,
    // downloaded_bytes, skipped, failures (lista), error, archive_dir,
    // started_at och finished_at. Se modules/podcast_archive.py.
    const res = await fetch("/api/spreaker/archive/status");
    if (!res.ok) return;
    const s = await res.json();
    renderArchiveStatus(s);
    // Aldrig två parallella frågeslingor, även om funktionen anropas från flera håll.
    clearTimeout(archivePollTimer);
    if (s.running) {
      // Pågår fortfarande: fråga igen om en sekund.
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

// "⬇️ Arkivera podden": starta en körning på servern och följ den.
document.getElementById("archiveRunBtn").addEventListener("click", async () => {
  // POST /api/spreaker/archive/run: starta en körning i bakgrunden. Svarar
  // direkt; själva arkiveringen tar minuter och följs via statusfrågorna.
  const res = await fetch("/api/spreaker/archive/run", { method: "POST" });
  // 409 = en körning pågår redan - inget fel, visa bara dess status.
  if (!res.ok && res.status !== 409) {
    const data = await res.json().catch(() => ({}));
    const status = document.getElementById("archiveStatus");
    status.className = "status error";
    status.textContent = `❌ ${data.detail || "Kunde inte starta arkiveringen."}`;
    return;
  }
  loadArchiveStatus();
});

// "⏹️ Avbryt": servern stannar efter pågående nedladdningsbit.
document.getElementById("archiveStopBtn").addEventListener("click", async () => {
  await fetch("/api/spreaker/archive/stop", { method: "POST" });
  loadArchiveStatus();
});

// ---------------------------------------------------------------------------
// Inställningsguide (fliken ⚙️ Inställningar)
// Fyller i .env via GUI:t och guidar Spreaker-OAuth. Sparade värden skrivs
// till .env och läses om live av backend (se routers/setup.py + config.reload).
// ---------------------------------------------------------------------------
// Token som hittats men ännu inte sparats. Den visas aldrig på sidan och
// skickas bara med när användaren klickar "Spara Spreaker-inställningar".
let discoveredSpreakerToken = null; // sätts av OAuth-utbytet/token-verifieringen

/**
 * Visar ett meddelande i inställningsflikens statusrad.
 * @param {string} message Texten.
 * @param {boolean|null} ok true = lyckat (grönt), false = fel (rött), null = pågår (grått).
 */
function setupStatus(message, ok) {
  const el = document.getElementById("setupStatus");
  // Meddelandet och en färg efter utfallet.
  el.textContent = message;
  el.className = ok === false ? "status error" : ok === true ? "status success" : "status";
}

/**
 * Fyller alla fält i fliken Inställningar med det som gäller just nu.
 * Hemligheter fylls aldrig i - fältet visar bara om något är sparat.
 */
async function loadSetupConfig() {
  try {
    // GET /api/setup/config: alla inställningar, med nycklar som motsvarar
    // .env-namnen i gemener (ollama_model = OLLAMA_MODEL). Hemligheter finns
    // bara som *_set (true/false) och *_masked (t.ex. "••••3xyz").
    const res = await fetch("/api/setup/config");
    if (!res.ok) return;
    // c = konfigurationen; fälten fylls i nedan, grupperat per ruta i fliken.
    const c = await res.json();

    // Spreaker: töm listan med shows, men visa det sparade show-id:t om det finns.
    document.getElementById("spShowSelect").innerHTML = "";
    document.getElementById("spSimulate").checked = c.spreaker_simulate;
    // Det sparade show-id:t visas som enda val tills ett nytt konto hämtats.
    if (c.spreaker_show_id) {
      const sel = document.getElementById("spShowSelect");
      sel.innerHTML = `<option value="${escapeHtml(c.spreaker_show_id)}">Nuvarande: ${escapeHtml(c.spreaker_show_id)}</option>`;
      document.getElementById("spShowBox").hidden = false;
    }

    // Nyckeln visas bara maskerad i platshållartexten - fältet självt är tomt.
    document.getElementById("openaiKey").placeholder = c.openai_api_key_set
      ? `Sparad (${c.openai_api_key_masked}) - lämna tomt för att behålla`
      : "sk-...";

    // Transkribering.
    document.getElementById("useLocalWhisper").checked = c.use_local_whisper;
    setSelect("localWhisperModel", c.local_whisper_model);
    setSelect("localAsrEngine", c.local_asr_engine);
    setSelect("whisperDevice", c.whisper_device);

    // AI-berikning. ?? "" gör att ett saknat värde blir ett tomt fält i stället för "null".
    setSelect("aiProvider", c.ai_provider);
    document.getElementById("ollamaHost").value = c.ollama_host || "";
    document.getElementById("ollamaModel").value = c.ollama_model || "";
    document.getElementById("aiTemperature").value = c.ai_temperature ?? "";
    document.getElementById("ollamaNumCtx").value = c.ollama_num_ctx ?? "";
    // Standardprompterna sparas för "Återställ standard" och för märkningen (standard/egen).
    promptDefaults.aiTitlePrompt = c.ai_title_prompt_default || "";
    promptDefaults.aiDescriptionPrompt = c.ai_description_prompt_default || "";
    document.getElementById("aiTitlePrompt").value = c.ai_title_prompt || "";
    document.getElementById("aiDescriptionPrompt").value = c.ai_description_prompt || "";
    updatePromptState("aiTitlePrompt");
    updatePromptState("aiDescriptionPrompt");

    // E-post.
    document.getElementById("emailEnabled").checked = c.email_enabled;
    document.getElementById("smtpHost").value = c.smtp_host || "";
    document.getElementById("smtpPort").value = c.smtp_port || "";
    document.getElementById("smtpUser").value = c.smtp_user || "";
    // Samma princip för SMTP-lösenordet: bara om det är sparat, aldrig själva värdet.
    document.getElementById("smtpPassword").placeholder = c.smtp_password_set
      ? "Sparat - lämna tomt för att behålla"
      : "app-lösenord";
    document.getElementById("notifyEmail").value = c.notify_email || "";

    // Lagring och loggning.
    document.getElementById("maxStored").value = c.max_stored_episodes || 0;
    setSelect("logLevel", c.log_level);
    document.getElementById("archiveDirInput").value = c.archive_dir || "";
    // Den fullständiga sökvägen, så en relativ inställning (t.ex. podcast_arkiv)
    // syns som den faktiska mappen på disken.
    document.getElementById("archiveDirResolved").textContent = c.archive_dir_resolved
      ? `Sparas i: ${c.archive_dir_resolved}`
      : "";
  } catch {
    // T.ex. om sidan öppnats från en annan dator (inställningarna är bara
    // tillgängliga lokalt, se routers/setup.py).
    setupStatus("Kunde inte läsa inställningarna.", false);
  }
}

/**
 * Väljer ett värde i en listruta.
 * @param {string} id Listrutans id.
 * @param {string|null} value Värdet; tomt eller null lämnar rutan orörd.
 */
function setSelect(id, value) {
  const el = document.getElementById(id);
  if (!el || value == null || value === "") return;
  // Ett värde från .env som inte finns i listan (t.ex. en annan Whisper-
  // modell) läggs till, så att det visas och inte skrivs över vid sparning.
  if (![...el.options].some((opt) => opt.value === value)) {
    el.add(new Option(`${value} (nuvarande)`, value));
  }
  // Välj värdet i listan.
  el.value = value;
}

/**
 * Fyller listan med kontots shows efter att en token hittats.
 * @param {{show_id: number, title: string}[]} shows
 */
function populateShowSelect(shows) {
  const sel = document.getElementById("spShowSelect");
  // Ett konto utan shows: visa det tydligt i stället för en tom lista.
  if (!shows.length) {
    sel.innerHTML = `<option value="">(Inga shows hittades på kontot)</option>`;
  } else {
    sel.innerHTML = shows
      .map((s) => `<option value="${escapeHtml(String(s.show_id))}">${escapeHtml(s.title)} (${escapeHtml(String(s.show_id))})</option>`)
      .join("");
  }
  document.getElementById("spShowBox").hidden = false;
}

// Steg 2 i Spreaker-guiden: bygg länken till Spreakers godkännandesida.
document.getElementById("spBuildUrlBtn").addEventListener("click", async () => {
  const clientId = document.getElementById("spClientId").value.trim();
  // Måste vara exakt samma adress som angavs när appen registrerades hos Spreaker.
  const redirectUri = document.getElementById("spRedirectUri").value.trim() || "http://localhost";
  // "return setupStatus(...)" visar felet och avbryter i samma rad.
  if (!clientId) return setupStatus("Fyll i Client ID först.", false);
  try {
    // Servern bygger adressen: https://www.spreaker.com/oauth2/authorize?
    // client_id=...&response_type=code&scope=basic&redirect_uri=...
    const res = await fetch("/api/setup/spreaker/authorize-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, redirect_uri: redirectUri }),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Kunde inte skapa länken.", false);
    const link = document.getElementById("spAuthLink");
    // Länken visas både som klickbar länk och som text (lätt att kopiera).
    link.href = data.url;
    link.textContent = data.url;
    // Visa länken; användaren öppnar den själv i en ny flik.
    document.getElementById("spAuthLinkWrap").hidden = false;
    setupStatus("Öppna länken, godkänn, och klistra tillbaka adressen nedan.", true);
  } catch {
    setupStatus("Nätverksfel när länken skulle skapas.", false);
  }
});

// Steg 3: byt koden (eller hela adressen från adressfältet) mot en token.
// Servern hämtar samtidigt kontots shows.
document.getElementById("spExchangeBtn").addEventListener("click", async () => {
  const body = {
    client_id: document.getElementById("spClientId").value.trim(),
    client_secret: document.getElementById("spClientSecret").value.trim(),
    redirect_uri: document.getElementById("spRedirectUri").value.trim() || "http://localhost",
    // Antingen själva koden eller hela adressen - servern plockar ut koden.
    code: document.getElementById("spCode").value.trim(),
  };
  if (!body.client_id || !body.client_secret || !body.code) {
    return setupStatus("Fyll i Client ID, Client Secret och koden/URL:en.", false);
  }
  setupStatus("Byter kod mot token...", null);
  try {
    // Servern byter koden mot en token hos Spreaker (koden gäller bara en gång
    // och en kort stund), kontrollerar token och listar kontots shows.
    // Svar: { token, user: { fullname, ... }, shows: [ { show_id, title } ] }
    const res = await fetch("/api/setup/spreaker/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) return setupStatus(data.detail || "Utbytet misslyckades.", false);
    // Token hålls bara i minnet tills användaren väljer show och sparar.
    discoveredSpreakerToken = data.token;
    populateShowSelect(data.shows || []);
    // Visa vems konto token gäller, så man ser att det är rätt konto.
    const name = (data.user && data.user.fullname) || "okänd användare";
    setupStatus(`✅ Token hämtad (inloggad som ${name}). Välj ditt show och spara.`, true);
  } catch {
    setupStatus("Nätverksfel vid utbytet.", false);
  }
});

// "Jag har redan en token": visa eller dölj fältet för att klistra in en.
document.getElementById("spHaveTokenBtn").addEventListener("click", () => {
  const box = document.getElementById("spTokenBox");
  // hidden-attributet döljer elementet utan någon CSS.
  box.hidden = !box.hidden;
});

// Kontrollera en inklistrad token och hämta kontots shows.
document.getElementById("spVerifyTokenBtn").addEventListener("click", async () => {
  const token = document.getElementById("spToken").value.trim();
  if (!token) return setupStatus("Klistra in en token först.", false);
  setupStatus("Verifierar token...", null);
  try {
    // Samma kontroll som efter utbytet, men för en token som redan fanns.
    // Svar: { user, shows }
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

// Spara Spreaker-inställningarna: simulering, valt show och (om en hittats) token.
document.getElementById("spSaveBtn").addEventListener("click", async () => {
  const values = {
    // Kryssrutor skickas som texten "true"/"false", precis som i .env.
    SPREAKER_SIMULATE: document.getElementById("spSimulate").checked ? "true" : "false",
  };
  const showId = document.getElementById("spShowSelect").value;
  // Tomma värden skickas inte, så redan sparade värden inte skrivs över.
  if (showId) values.SPREAKER_SHOW_ID = showId;
  // Token skickas bara om en ny hittats - annars behåller servern den sparade.
  if (discoveredSpreakerToken) values.SPREAKER_API_TOKEN = discoveredSpreakerToken;
  await saveSettings(values, "Spreaker-inställningar sparade.");
});

// Prova en OpenAI-nyckel utan att spara den.
document.getElementById("openaiVerifyBtn").addEventListener("click", async () => {
  const key = document.getElementById("openaiKey").value.trim();
  if (!key) return setupStatus("Fyll i en nyckel att verifiera.", false);
  setupStatus("Verifierar OpenAI-nyckel...", null);
  try {
    // Servern gör ett gratis provanrop mot OpenAI med nyckeln.
    // Svar: { valid: true }, eller ett fel om nyckeln avvisades.
    const res = await fetch("/api/setup/openai/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json();
    // Nyckeln sparas inte här - bara med "Spara alla inställningar".
    setupStatus(res.ok ? "✅ OpenAI-nyckeln fungerar." : data.detail || "Nyckeln avvisades.", res.ok);
  } catch {
    setupStatus("Nätverksfel vid verifieringen.", false);
  }
});

// AI-prompter: fälten visar alltid prompten som används (egen eller
// standard). "Återställ standard" lägger tillbaka standardtexten, som
// sparas som tom i .env (se routers/setup.py).
// Standardprompterna per fält-id (fylls av loadSetupConfig).
const promptDefaults = { aiTitlePrompt: "", aiDescriptionPrompt: "" };
// Var märkningen (standard/egen) visas för respektive promptfält.
const PROMPT_STATE_IDS = { aiTitlePrompt: "aiTitlePromptState", aiDescriptionPrompt: "aiDescriptionPromptState" };

/**
 * Visar "(standard)" eller "(egen - ändrad från standard)" bredvid ett promptfält.
 * @param {string} id Promptfältets id.
 */
function updatePromptState(id) {
  // Samma normalisering som servern gör, så jämförelsen ger samma svar.
  const value = document.getElementById(id).value.replace(/\r\n/g, "\n").trim();
  // Ett tomt fält räknas också som standard (servern använder då standardprompten).
  const isDefault = !value || value === promptDefaults[id].trim();
  document.getElementById(PROMPT_STATE_IDS[id]).textContent = isDefault ? "(standard)" : "(egen - ändrad från standard)";
}

// Märkningen uppdateras medan man skriver i promptfälten.
Object.keys(PROMPT_STATE_IDS).forEach((id) => {
  document.getElementById(id).addEventListener("input", () => updatePromptState(id));
});

// "↩️ Återställ standard": knappens data-target anger vilket fält den gäller.
document.querySelectorAll(".prompt-reset-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const id = btn.dataset.target;
    // Lägg tillbaka standardprompten - sparas först med "Spara alla inställningar".
    document.getElementById(id).value = promptDefaults[id];
    updatePromptState(id);
  });
});

// "💾 Spara alla inställningar": samla alla fält och skicka dem på en gång.
// Nycklarna är samma namn som i .env. Servern kontrollerar värdena.
document.getElementById("setupSaveAllBtn").addEventListener("click", async () => {
  // Alla vanliga fält skickas alltid - även oförändrade - så att det som
  // står i formuläret blir det som gäller. Tomma talfält får standardvärden
  // (t.ex. SMTP-port 587). Temperatur och kontextfönster skickas som de är;
  // tomt betyder "standardvärdet" på servern.
  const values = {
    USE_LOCAL_WHISPER: document.getElementById("useLocalWhisper").checked ? "true" : "false",
    LOCAL_WHISPER_MODEL: document.getElementById("localWhisperModel").value,
    LOCAL_ASR_ENGINE: document.getElementById("localAsrEngine").value,
    WHISPER_DEVICE: document.getElementById("whisperDevice").value,
    AI_PROVIDER: document.getElementById("aiProvider").value,
    OLLAMA_HOST: document.getElementById("ollamaHost").value.trim(),
    OLLAMA_MODEL: document.getElementById("ollamaModel").value.trim(),
    AI_TEMPERATURE: document.getElementById("aiTemperature").value.trim(),
    OLLAMA_NUM_CTX: document.getElementById("ollamaNumCtx").value.trim(),
    EMAIL_ENABLED: document.getElementById("emailEnabled").checked ? "true" : "false",
    SMTP_HOST: document.getElementById("smtpHost").value.trim(),
    SMTP_PORT: document.getElementById("smtpPort").value.trim() || "587",
    SMTP_USER: document.getElementById("smtpUser").value.trim(),
    NOTIFY_EMAIL: document.getElementById("notifyEmail").value.trim(),
    MAX_STORED_EPISODES: document.getElementById("maxStored").value.trim() || "0",
    LOG_LEVEL: document.getElementById("logLevel").value,
    ARCHIVE_DIR: document.getElementById("archiveDirInput").value.trim() || "podcast_arkiv",
    // Backend sparar en prompt som är identisk med standarden som tom.
    AI_TITLE_PROMPT: document.getElementById("aiTitlePrompt").value,
    AI_DESCRIPTION_PROMPT: document.getElementById("aiDescriptionPrompt").value,
  };
  // Hemligheter skickas bara om användaren faktiskt skrivit något (annars
  // behåller backend det sparade värdet, se routers/setup.py).
  const openaiKey = document.getElementById("openaiKey").value.trim();
  // Ett tomt nyckelfält betyder "ändra inte".
  if (openaiKey) values.OPENAI_API_KEY = openaiKey;
  const smtpPassword = document.getElementById("smtpPassword").value.trim();
  if (smtpPassword) values.SMTP_PASSWORD = smtpPassword;
  await saveSettings(values, "Alla inställningar sparade.");
});

/**
 * Skickar inställningar till servern, som skriver .env och läser in den på nytt.
 * @param {object} values {"NYCKEL": "värde"} - samma namn som i .env.
 * @param {string} successMessage Visas när sparningen lyckats.
 */
async function saveSettings(values, successMessage) {
  setupStatus("Sparar...", null);
  try {
    // POST /api/setup/save med { values: { NYCKEL: "värde" } }. Servern
    // tillåter bara kända nycklar, kontrollerar värdena, skriver .env och
    // läser in den igen - ändringarna gäller direkt. Svaret är den nya
    // konfigurationen (samma form som GET /api/setup/config).
    const res = await fetch("/api/setup/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await res.json();
    // Serverns förklaring visas, t.ex. "Temperaturen måste vara ett tal mellan 0 och 2."
    if (!res.ok) return setupStatus(data.detail || "Kunde inte spara.", false);
    setupStatus(`✅ ${successMessage}`, true);
    // Token är nu sparad på servern - glöm den här och töm fälten.
    discoveredSpreakerToken = null;
    // Töm fälten med token och kod - de behövs inte längre.
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
// Ikon per stegstatus i stegvyn för det jobb som körs.
const QUEUE_STEP_ICONS = { pending: "⏳", running: "⚙️", done: "✅", skipped: "⏭️", error: "❌" };
// Text per jobbstatus under varje rad i kön.
const QUEUE_STATUS_LABELS = {
  queued: "⏳ I kö",
  running: "⚙️ Bearbetar...",
  done: "✅ Klar",
  error: "❌ Fel",
  cancelled: "🚫 Avbruten",
};

// Köns läge vid senaste hämtningen - avgör om knappen pausar eller startar.
let queuePaused = false;
// Antal klara jobb vid förra hämtningen - när det ökar hämtas ny statistik.
let lastKnownDoneCount = 0;

/**
 * HTML för stegvyn (klippning, transkribering ...) med en mätare per steg.
 * @param {object[]} steps Stegen från servern (label, status, percent).
 * @returns {string}
 */
function renderQueueSteps(steps) {
  // Varje steg blir två rader: namn med ikon och procent, och en mätare.
  // style="width: X%" gör mätarens fyllning lika bred som procenten.
  return (steps || [])
    .map((s) => {
      // Okänd status visas som väntande.
      const icon = QUEUE_STEP_ICONS[s.status] || "⏳";
      // Mätarens färg följer stegets status (grön/röd/grå i style.css).
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

/**
 * HTML för "Beräknat klart om ~X" för det jobb som körs.
 * @param {object} it Köraden.
 * @returns {string}
 */
function renderEtaTimer(it) {
  // Ingen uppskattning finns innan första predikan bearbetats klart.
  if (!it.estimated_completion_at) {
    return `<div class="queue-eta queue-eta-unknown">⏱️ Ingen tidsuppskattning tillgänglig än (ingen bearbetningshistorik).</div>`;
  }
  // Återstående tid räknas om vid varje uppdatering (var 1,5 s).
  const remainingMs = new Date(it.estimated_completion_at).getTime() - Date.now();
  // Uppskattningen har passerats - jobbet pågår fortfarande, det har inte fastnat.
  if (remainingMs <= 0) {
    return `<div class="queue-eta">⏱️ Tar längre än beräknat...</div>`;
  }
  return `<div class="queue-eta">⏱️ Beräknat klart om ~${formatDuration(remainingMs / 1000)}</div>`;
}

/**
 * Visar antalet på rensa-knapparna och spärrar dem som inte har något att rensa.
 * @param {object[]} items Köns rader.
 */
function updateQueueClearButtonCounts(items) {
  // Räkna hur många rader varje rensa-knapp skulle ta bort.
  const doneCount = items.filter((it) => it.status === "done").length;
  const errorCount = items.filter((it) => it.status === "error" || it.status === "cancelled").length;
  // "Rensa allt" rör aldrig det jobb som körs - det räknas inte heller.
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

/**
 * Ritar om hela kön: en rad per objekt med mätare, status, resultat eller
 * fel, och knapparna som passar objektets läge.
 * @param {object[]} items Köns rader från GET /api/queue.
 */
function renderQueueList(items) {
  // Knapparnas antal uppdateras varje gång listan ritas.
  updateQueueClearButtonCounts(items);

  const list = document.getElementById("queueList");
  // Tom kö: en förklarande rad i stället för en tom lista.
  if (!items.length) {
    list.innerHTML = `<li class="queue-empty">Inget i kön just nu.</li>`;
    return;
  }

  // En <li> per objekt i kön:
  //   - rubrikrad: filnamn/titel, talare, etikett (Manuell/CSV/AI) och procent
  //   - en mätare för hela jobbet
  //   - statustext (I kö, Bearbetar..., Klar, Fel, Avbruten)
  //   - innehåll beroende på status: stegvy, resultat eller felmeddelande
  //   - knappar (Avbryt, Prioritera, Ta bort) med jobbets id i data-attribut
  // Allt från servern som kan innehålla användartext går genom escapeHtml.
  list.innerHTML = items
    .map((it) => {
      // Hela jobbets procent (viktat snitt av stegen, räknat på servern).
      const percent = it.overall_percent || 0;
      const fillClass = ["error", "done", "cancelled"].includes(it.status) ? it.status : "";
      // Liten etikett som visar varifrån jobbet kom.
      const kindLabel = { bulk: "CSV", regenerate: "AI" }[it.kind] || "Manuell";
      // Text för statusen, t.ex. "✅ Klar".
      const statusLabel = QUEUE_STATUS_LABELS[it.status] || "";

      let body = "";
      let actions = "";
      // Pågående: tidsuppskattning, stegvy och Avbryt-knapp.
      if (it.status === "running") {
        body = `${renderEtaTimer(it)}<div class="queue-steps">${renderQueueSteps(it.steps)}</div>`;
        actions = `<button type="button" class="queue-cancel-btn" data-job-id="${it.job_id}">🚫 Avbryt</button>`;
      // "Generera om"-jobb: förslaget granskas i Spreaker-fliken, inte här.
      } else if (it.status === "done" && it.kind === "regenerate" && it.result) {
        body = `
          <div class="queue-item-result">
            Nytt förslag genererat - granska och spara i "Hantera Spreaker"-fliken.
          </div>`;
      // Publicerat: titel, länk och taggar.
      } else if (it.status === "done" && it.result) {
        // Taggarna visas som små "piller" (.tag-pill i style.css).
        const tagsHtml = (it.result.tags || [])
          .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`)
          .join(" ");
        body = `
          <div class="queue-item-result">
            <strong>${escapeHtml(it.result.final_title)}</strong><br>
            <a href="${escapeHtml(it.result.episode_url)}" target="_blank">${escapeHtml(it.result.episode_url)}</a>
            ${tagsHtml ? `<div>${tagsHtml}</div>` : ""}
          </div>`;
      // Fel eller avbrutet: visa orsaken från servern.
      } else if (it.status === "error" || it.status === "cancelled") {
        body = `<div class="queue-item-error">${escapeHtml(it.error || "Okänt fel")}</div>`;
      }

      // Väntande jobb kan flyttas först; allt utom det pågående kan tas bort.
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
// Avbryt, Ta bort och Prioritera. Knappen spärras direkt, så att ett
// dubbelklick inte skickar två anrop, och kön hämtas om efteråt.
document.getElementById("queueList").addEventListener("click", async (e) => {
  // Högst en av dessa är satt - den knapp som klickades.
  const cancelBtn = e.target.closest(".queue-cancel-btn");
  const removeBtn = e.target.closest(".queue-remove-btn");
  const prioritizeBtn = e.target.closest(".queue-prioritize-btn");

  if (cancelBtn) {
    cancelBtn.disabled = true;
    // Visa direkt att avbrottet är på väg - det kan ta någon sekund.
    cancelBtn.textContent = "Avbryter...";
    try {
      // Avbrottet sker på servern: pågående transkribering stoppas direkt, andra
      // steg vid nästa steggräns. Kön visar "Avbruten" vid nästa uppdatering.
      await fetch(`/api/queue/cancel/${cancelBtn.dataset.jobId}`, { method: "POST" });
    } finally {
      loadQueue();
    }
  } else if (removeBtn) {
    removeBtn.disabled = true;
    try {
      // DELETE /api/queue/{queue_id}: tar bort raden ur listan. Filer som redan
      // skapats påverkas inte. Går inte för ett jobb som körs (409).
      const res = await fetch(`/api/queue/${removeBtn.dataset.queueId}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json();
        // T.ex. om objektet hunnit börja bearbetas sedan listan ritades.
        alert(err.detail || "Kunde inte ta bort objektet.");
      }
    } finally {
      loadQueue();
    }
  } else if (prioritizeBtn) {
    prioritizeBtn.disabled = true;
    try {
      // Flyttar objektet först i kön - det bearbetas härnäst.
      await fetch(`/api/queue/prioritize/${prioritizeBtn.dataset.queueId}`, { method: "POST" });
    } finally {
      loadQueue();
    }
  }
});

// Rensa-knapparna: servern tar bort raderna, sedan hämtas kön om.
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
  // "Rensa allt" tar även bort väntande jobb - fråga först.
  if (!confirm("Rensa hela kön? Objekt som redan bearbetas påverkas inte, men allt annat (väntande, klara och misslyckade) tas bort från listan.")) {
    return;
  }
  try {
    await fetch("/api/queue/clear", { method: "POST" });
  } finally {
    loadQueue();
  }
});

/**
 * Hämtar kön från servern och ritar om den. Körs var 1,5 sekund (se
 * setInterval längst ned) och direkt efter varje knapptryck i kön.
 */
async function loadQueue() {
  try {
    // GET /api/queue svarar med
    //   { paused: bool, current_queue_id: "..." eller null, items: [...] }
    // där varje objekt har samma form som i services/pipeline.queue_item_view.
    const res = await fetch("/api/queue");
    if (!res.ok) return;
    const state = await res.json();

    // Knappen och etiketten "kör/pausad" följer köns läge på servern.
    queuePaused = state.paused;
    // Etiketten och knappens text visar köns läge.
    document.getElementById("queueStateLabel").textContent = state.paused ? "pausad" : "kör";
    document.getElementById("queueToggleBtn").textContent = state.paused ? "▶ Starta" : "⏸ Pausa";

    renderQueueList(state.items || []);

    const doneCount = (state.items || []).filter((it) => it.status === "done").length;
    // Ett jobb har blivit klart sedan sist - statistiken har ändrats.
    if (doneCount > lastKnownDoneCount) loadStats();
    lastKnownDoneCount = doneCount;
  } catch {
    // Kön är en kompletterande vy - fel här ska aldrig blockera resten av appen.
  }
}

// "⏸ Pausa" / "▶ Starta": samma knapp, beroende på köns läge.
document.getElementById("queueToggleBtn").addEventListener("click", async () => {
  // Pausad kö startas, körande kö pausas. Ett jobb som redan pågår får alltid bli klart.
  const endpoint = queuePaused ? "/api/queue/resume" : "/api/queue/pause";
  try {
    await fetch(endpoint, { method: "POST" });
  } finally {
    loadQueue();
  }
});

// Uppdatera kön var 1,5 sekund, och en gång direkt när sidan laddas.
setInterval(loadQueue, 1500);
loadQueue();
