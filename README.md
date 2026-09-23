# Predikan → Podcast

Lokal webbapplikation för att klippa, transkribera, AI-berika och publicera
predikor till Spreaker.

## Arkitektur

```
predikan-app/
├── app.py                    # App-sammansättning: skapar FastAPI-appen, kopplar in routrarna, startar kö-arbetartråden
├── config.py                 # Läser in .env
├── requirements.txt
├── requirements-dev.txt      # + pytest/ruff/mypy (se avsnitt 13)
├── pyproject.toml            # Konfiguration för ruff/mypy/pytest
├── .env-example               # Mall för dina inställningar (kopiera till .env)
├── routers/                  # HTTP-endpoints, ett API-område per fil
│   ├── upload.py              # POST /api/upload, GET /api/audio/{file_id}
│   ├── process.py             # POST /api/process, GET /api/process/status/{job_id}
│   ├── queue.py                # GET/POST/DELETE /api/queue/... (se avsnitt 6)
│   ├── bulk_import.py         # POST /api/bulk-import + CSV-validering (se avsnitt 11)
│   ├── spreaker_episodes.py   # Hantera Spreaker + podd-arkivet (se avsnitt 16-17)
│   ├── setup.py               # Inställningsguiden (fliken ⚙️ Inställningar)
│   └── stats.py                # GET /api/stats
├── services/
│   ├── state.py                # Delat, processlokalt runtime-tillstånd (inte i databasen)
│   └── pipeline.py             # Själva bearbetningspipelinen + kö-arbetartråden
├── modules/
│   ├── audio_processor.py    # Klippning + normalisering (pydub/ffmpeg)
│   ├── transcription.py      # Whisper (OpenAI API eller lokalt)
│   ├── transcription_worker.py         # Kör transkriberingen i en avbrytbar bakgrundsprocess (se avsnitt 6)
│   ├── transcription_worker_process.py # Startpunkt för den bakgrundsprocessen
│   ├── ai_enrichment.py      # GPT: titel/beskrivning/taggar
│   ├── spreaker_client.py    # Spreaker API-uppladdning (+ simuleringsläge)
│   ├── spreaker_episode_store.py # Lokal cache av avsnitten på Spreaker-kontot (avsnitt 16)
│   ├── podcast_archive.py    # Lokalt podd-arkiv: mp3/xml/txt/transkript per avsnitt (avsnitt 17)
│   ├── env_file.py           # Skriver .env åt inställningsguiden
│   ├── email_notifier.py     # Bekräftelsemail
│   ├── db.py                  # SQLite-anslutning + schema (se avsnitt 8)
│   ├── queue_store.py         # Beständig bearbetningskö (databaslager för avsnitt 6)
│   ├── episode_store.py       # Episodhistorik, statistik (avsnitt 9) och lagringsrensning (avsnitt 10)
│   ├── storage_cleanup.py    # Ad-hoc-städning av ett enskilt misslyckat/avbrutet försök
│   └── app_logging.py        # Loggkonfiguration (se avsnitt 12)
├── tests/                     # pytest-svit (se avsnitt 13)
├── static/
│   ├── index.html            # Frontend (uppladdning, vågform, formulär)
│   ├── style.css
│   └── app.js                 # Wavesurfer.js-integration + API-anrop
├── uploads/                   # Original-filer (skapas automatiskt)
├── processed/                 # Klippta/färdiga filer (skapas automatiskt)
├── bulk_import/                # Ljudfiler för CSV-bulkimport (se avsnitt 11)
├── podcast_arkiv/              # Lokalt podd-arkiv (skapas vid första arkiveringen, se avsnitt 17)
├── predikan.db                 # SQLite-databas (skapas automatiskt, se avsnitt 8)
└── app.log                    # Loggfil (skapas automatiskt, se avsnitt 12)
```

## 1. Förutsättningar

- **Python 3.10+**
- **ffmpeg** installerat och tillgängligt i PATH (krävs av pydub för att
  läsa/skriva ljud - se listan över stödda uppladdningsformat i avsnitt 5):
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: ladda ner från https://ffmpeg.org/download.html och lägg till i PATH

Kontrollera installationen:
```bash
ffmpeg -version
```

## 2. Installation

### Snabbast: install-skript (rekommenderas)

Från projektroten - skapar venv, installerar appen (så kommandot `predikan`
blir tillgängligt), kopierar `.env-example` → `.env` och skriver ut nästa steg:

```powershell
.\setup.ps1
```

```bash
./setup.sh
```

Lägg till `-NoWhisper` (PowerShell) eller `--no-whisper` (bash) för att hoppa
över lokal Whisper och i stället använda OpenAI Whisper API. Resten av
uppgifterna (Spreaker-token, Show-ID, OpenAI-nyckel m.m.) fyller du i via
fliken **⚙️ Inställningar** i webbappen efter start - se avsnitt 3.

### Manuellt (om du hellre gör stegen själv)

```bash
cd predikan-app
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Windows + PowerShell:** om `venv\Scripts\activate` ger felet "running
scripts is disabled on this system" (PowerShells execution policy, en
säkerhetsfunktion du inte bör stänga av globalt), slipper du aktivera
venv helt genom att peka direkt på dess `python.exe` istället:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m uvicorn app:app --reload
```

Fungerar precis likadant som en aktiverad venv, utan att röra några
säkerhetsinställningar.

### Alternativ: installera som paket (ger kommandot `predikan`)

I stället för `requirements.txt` kan appen installeras som ett paket i
utvecklingsläge (`-e`) direkt från repo-roten. Då registreras ett
`predikan`-kommando som startar servern var du än står i terminalen:

```bash
pip install -e .                    # bara OpenAI Whisper API
pip install -e ".[faster-whisper]"  # + lokal, offline-transkribering (rekommenderas)
pip install -e ".[whisper]"         # + lokal transkribering via openai-whisper
pip install -e ".[dev]"             # + pytest/ruff/mypy för utveckling
```

Utvecklingsläge (`-e`) används medvetet: appen läser statiska filer och
skapar `uploads/`, `processed/`, `bulk_import/` och `predikan.db` relativt
sin egen plats i repot, så filerna ska ligga kvar där.

## 3. Konfiguration

```bash
cp .env-example .env
```

Öppna `.env` och fyll i - eller starta appen och fyll i det mesta under
fliken **⚙️ Inställningar**, som skriver `.env` åt dig. De flesta
inställningar som sparas där gäller direkt, utan omstart.

### Helt offline-läge (rekommenderas om du vill slippa OpenAI helt)

Standardvärdena i `.env-example` är redan inställda för offline-drift:
`USE_LOCAL_WHISPER=true` och `AI_PROVIDER=ollama`. Så här sätter du upp det:

**Transkribering (lokal Whisper):**
`openai-whisper` ingår redan i `requirements.txt` och installerades i steg 2
ovan. Inget mer behövs - `USE_LOCAL_WHISPER=true` i `.env` räcker. Modellen
(`LOCAL_WHISPER_MODEL=small` som standard) laddas ner automatiskt första
gången och körs sedan helt offline.

**AI-berikning (titel/beskrivning/taggar via Ollama):**
1. Installera Ollama: https://ollama.com/download (Windows/macOS/Linux)
2. Ladda ner en modell, t.ex.:
   ```bash
   ollama pull llama3.1
   ```
3. Se till att `AI_PROVIDER=ollama` och `OLLAMA_MODEL=llama3.1` står i `.env`.
   Ollama startar oftast som en bakgrundstjänst automatiskt efter
   installation - annars kör `ollama serve` i en egen terminal.

Med detta uppsatt lämnar ingen ljuddata eller text din dator under
transkribering eller AI-berikning. Endast själva **publiceringen till
Spreaker** kräver internet (och `SPREAKER_SIMULATE=true` om du vill testa
även det offline).

### Molnbaserat läge (OpenAI)

- **OPENAI_API_KEY** – krävs om du använder OpenAI Whisper API och/eller GPT
  för AI-berikning. Skaffa en nyckel på https://platform.openai.com/api-keys
  - Sätt `USE_LOCAL_WHISPER=false` för att använda Whisper API istället för lokal modell.
  - Sätt `AI_PROVIDER=openai` för att använda GPT istället för Ollama.
- **SPREAKER_API_TOKEN** och **SPREAKER_SHOW_ID** – för riktig publicering.
  Spreaker har ingen enkel "kopiera nyckel"-knapp - du behöver gå igenom en
  kort OAuth-procedur. Se avsnittet "Så här skaffar du Spreaker-uppgifter"
  nedan för en komplett steg-för-steg-guide.
  - Saknar du nycklar än så länge? Lämna `SPREAKER_SIMULATE=true` (standard) så
    simuleras publiceringen och du kan testa hela flödet ändå.
- **E-post (valfritt)** – sätt `EMAIL_ENABLED=true` och fyll i SMTP-uppgifter
  om du vill ha ett riktigt bekräftelsemail. Annars visas en sammanfattning i
  webbläsaren istället, vilket räcker fint för v1.
- **MAX_STORED_EPISODES** (valfritt) – begränsar hur många predikningar som
  sparas i `uploads/` + `processed/` samtidigt. Se avsnitt 10 nedan.
- **BULK_IMPORT_DIR** (valfritt, standard `bulk_import/`) – mapp där
  ljudfiler för CSV-bulkimport ska ligga. Se avsnitt 11 nedan.
- **LOG_LEVEL** / **LOG_FILE** (valfritt) – styr loggfilen (`app.log` som
  standard). Se avsnitt 12 nedan.
- **DATABASE_FILE** (valfritt, standard `predikan.db`) – SQLite-databasen
  för bearbetningskön och episodhistoriken/statistiken. Se avsnitt 8 nedan.
- **ARCHIVE_DIR** (valfritt, standard `podcast_arkiv/`) – mapp för det
  lokala podd-arkivet. Kan vara en absolut sökväg på en annan disk, t.ex.
  `ARCHIVE_DIR=D:/Podcastarkiv`. Se avsnitt 17 nedan.
- **AI_TITLE_PROMPT** / **AI_DESCRIPTION_PROMPT** (valfritt) – egna prompter
  för AI-genererad titel och beskrivning. Tomt = inbyggd standardprompt. Se
  "Egna AI-prompter" nedan.
- **AI_TEMPERATURE** (valfritt, standard `0.2`) – hur fritt AI:n formulerar
  sig, 0-1. Lågt ger trognare och mer förutsägbara sammanfattningar.
- **OLLAMA_NUM_CTX** (valfritt, standard `16384`) – hur många tokens Ollama
  läser åt gången. Ollamas eget standardvärde räcker bara till en bråkdel av
  en predikan (resten klipps tyst bort); 16384 rymmer ca 36 000 tecken
  transkript. Längre transkript kortas i mitten, så början och slutet av
  predikan alltid kommer med. Ett större värde kräver mer minne.
- **MAX_UPLOAD_MB** (valfritt, standard `500`) – största tillåtna
  filuppladdning i MB, som skydd mot att en jättefil fyller disken.
  `0` = ingen gräns.
- **SETUP_ALLOW_REMOTE** (valfritt, standard `false`) – inställningsguiden
  skriver `.env` utan egen inloggning och nås därför som standard bara från
  samma dator. Sätt `true` bara bakom en omvänd proxy med inloggning (i
  Docker sätter `docker-compose.yml` den åt dig, se avsnitt 15).

### Egna AI-prompter

Prompterna som styr vad AI:n skriver som titel och beskrivning kan ändras
under **⚙️ Inställningar → 🤖 AI-berikning**. Fälten visar prompten som
används just nu, och bredvid rubriken står om den är **(standard)** eller
**(egen)**. **↩️ Återställ standard** lägger tillbaka appens inbyggda prompt.
Ändringen gäller direkt när du sparar.

Platshållare som fylls i innan prompten skickas till AI:n:

| Platshållare      | Ersätts med                                             | Titel | Beskrivning |
|-------------------|---------------------------------------------------------|:-----:|:-----------:|
| `{speaker}`       | Talarens namn                                           | ✓     | ✓           |
| `{transcript}`    | Transkriptet (**måste finnas med**)                     | ✓     | ✓           |
| `{fallback_text}` | Reservtexten när transkriptet inte går att sammanfatta  |       | ✓           |

Andra klammerparenteser i prompten lämnas orörda. En prompt utan
`{transcript}` sparas inte, eftersom AI:n då aldrig skulle få se predikan.

Prompterna sparas som `AI_TITLE_PROMPT` / `AI_DESCRIPTION_PROMPT` i `.env`,
med radbrytningar skrivna som `\n` inom citattecken. En prompt som är
identisk med standarden sparas som tom, så framtida förbättringar av
standardprompten slår igenom automatiskt.

Med standardprompten för beskrivning finns en extra kontroll som gör ett
nytt försök om AI:n råkat skriva av instruktionerna i stället för att följa
dem. Den kontrollen letar efter fraser ur just standardprompten och körs
därför inte med en egen prompt.

### Så här skaffar du Spreaker-uppgifter (SPREAKER_API_TOKEN + SPREAKER_SHOW_ID)

> **Enklast: använd inställningsguiden.** Starta appen, öppna fliken
> **⚙️ Inställningar → 📡 Spreaker**. Guiden bygger auktoriseringslänken åt
> dig, byter koden mot en token (ingen curl behövs), verifierar den och
> **listar dina shows så du bara klickar rätt** - inget manuellt sökande
> efter Show-ID. Det enda du gör för hand är att registrera appen (steg 1
> nedan) för att få Client ID + Client Secret. Stegen nedan beskriver samma
> procedur manuellt, om du föredrar det.

1. **Aktivera utvecklarläge:** logga in på spreaker.com och besök
   https://www.spreaker.com/account/developer/enable
   Registrera en ny "app" (bara för eget bruk). Du får ett **Client ID** och
   en **Client Secret** - spara båda. Som "Redirect URI" kan du ange
   `http://localhost` (den behöver inte vara nåbar, den används bara för att
   fånga upp svaret i webbläsarens adressfält).

2. **Hämta en auktoriseringskod:** klistra in i webbläsaren (byt ut
   `APP_CLIENT_ID`):
   ```
   https://www.spreaker.com/oauth2/authorize?client_id=APP_CLIENT_ID&response_type=code&state=xyz123&scope=basic&redirect_uri=http://localhost
   ```
   Logga in, klicka "Tillåt". Sidan du hamnar på visar troligen ett fel -
   det är förväntat. Kopiera istället koden ur adressfältet, efter `code=`.

3. **Byt koden mot en token** (PowerShell/terminal, `curl.exe` finns
   inbyggt i Windows):
   ```bash
   curl.exe -X POST -F "grant_type=authorization_code" -F "client_id=APP_CLIENT_ID" -F "client_secret=APP_CLIENT_SECRET" -F "redirect_uri=http://localhost" -F "code=KODEN_FRÅN_STEG_2" https://api.spreaker.com/oauth2/token
   ```
   Svaret innehåller `access_token` - klistra in det som `SPREAKER_API_TOKEN`
   i `.env`. Tokenet gäller i praktiken flera år, så detta görs bara en gång.

4. **Hitta ditt Show ID:** enklast är att öppna din podcast på spreaker.com
   och läsa av numret i webbadressen, t.ex. `spreaker.com/show/1234567` →
   `1234567`. Klistra in det som `SPREAKER_SHOW_ID`.

5. Sätt `SPREAKER_SIMULATE=false` i `.env` och starta om servern.

## 4. Starta applikationen

```bash
uvicorn app:app --reload
```

Har du installerat appen som paket (se avsnitt 2) kan du i stället köra:

```bash
predikan
```

Kommandot startar samma server. Host/port styrs av miljövariablerna
`HOST` (standard `127.0.0.1`) och `PORT` (standard `8000`), och
`RELOAD=1` slår på autoreload vid kodändring under utveckling.

Öppna sedan: **http://127.0.0.1:8000**

(Vill du köra allt i en container istället för en venv? Se avsnitt 15.)

## 5. Användarflöde

1. **Ladda upp** en ljudfil - stödda format: `.mp3`, `.wav`, `.m4a`,
   `.aac`, `.ogg`, `.opus`, `.flac`, `.wma`. Alla normaliseras till mp3
   redan i klippningssteget, så originalformatet spelar ingen roll för
   resten av pipelinen.
2. **Klipp** predikan: spela upp ljudet, dra i den blå markeringen i vågformen
   (eller använd "Sätt start/slut = nuvarande tid"-knapparna) för att välja
   exakt vilket avsnitt som ska publiceras.
3. Fyll i **Talare** (obligatoriskt). Lämna **Titel** och **Beskrivning**
   tomma om du vill att AI ska generera dem automatiskt utifrån
   transkriberingen. **Publiceringsdatum** är valfritt och har dubbel
   funktion: ett **framtida** datum schemalägger avsnittet, medan
   **dagens datum eller ett datum bakåt i tiden** bakåtdaterar avsnittet
   till det datumet (praktiskt för äldre inspelningar). Lämna tomt för att
   publicera direkt med dagens datum.
4. Klicka **"➕ Lägg till i kö"**. Predikan läggs till i **bearbetningskön**
   (se kolumnen till höger, och avsnitt 6 nedan) istället för att bearbetas
   direkt - formuläret återställs omedelbart så du kan ladda upp och klippa
   nästa predikan direkt, utan att vänta. Kön kör sedan varje predikan i tur
   och ordning:
   - klipper och volymnormaliserar ljudet
   - transkriberar det till text
   - genererar titel och/eller beskrivning med AI (bara för fält du lämnat
     tomma - tre separata, oberoende frågor för titel/beskrivning/taggar,
     istället för en enda stor fråga) och väljer alltid taggar
   - laddar upp och publicerar (eller schemalägger) avsnittet på Spreaker
   - skickar bekräftelse (e-post eller sammanfattningssida)
5. I kökolumnen ser du **en procentmätare per steg** för den predikan som
   just nu bearbetas (samt en sammanvägd totalprocent). Spreaker-
   uppladdningen visar verklig, exakt procent baserat på hur mycket av
   filen som skickats. Övriga steg (transkribering, AI-berikning m.m.) visar
   en uppskattad procent baserat på ljudlängd och en tumregel för hastighet,
   eftersom de biblioteken inte rapporterar exakt framdrift internt. När en
   predikan är klar visas titel, taggar och länk till det publicerade
   avsnittet direkt i kön.

**Mörkt läge:** sidan följer automatiskt systemets/webbläsarens
ljus/mörkt-inställning. Knappen (🌙/☀️) uppe i högra hörnet låter dig
uttryckligen välja ett läge istället - valet sparas i webbläsaren
(`localStorage`) och gäller sedan oavsett systeminställning tills du
byter tillbaka.

## 6. Bearbetningskö (pausa/starta)

Alla predikningar - både manuellt klippta och rader från CSV-bulkimport
(avsnitt 11) - hamnar i **samma bearbetningskö**, i den ordning de lades
till. Kön bearbetar bara **en predikan i taget**, så aldrig mer än en tung
Whisper/Ollama/Spreaker-körning pågår samtidigt - det är själva poängen,
för att kunna styra hur mycket av datorns resurser bearbetningen tar vid
lokal körning. Kön sparas i databasen (se avsnitt 8) och **överlever en
omstart av servern** - även pausläget.

Kolumnen till höger visar kön live (pollas var 1,5 sekund) med status,
procent och (för den som bearbetas just nu) samma detaljerade stegvy som
tidigare. Knappen **"⏸ Pausa" / "▶ Starta"** styr om kön ska plocka upp
nästa väntande predikan:

- **Pausad:** inget nytt objekt påbörjas. Praktiskt för att i lugn och ro
  klippa och lägga till flera predikningar utan att belasta datorn
  förrän du är redo - klicka sedan "Starta" för att bearbeta hela kön i
  ett svep.
- **Kör** (standard): nästa väntande predikan i kön påbörjas så snart
  föregående är klar.

**Avbryta ett pågående jobb:** en predikan som redan påbörjats stoppas
inte av att kön pausas - klicka istället **"🚫 Avbryt"** på den (syns när
den är markerad "Bearbetar..."). Sitter jobbet i transkriberingssteget -
det klart mest tidskrävande, särskilt med lokal Whisper på CPU - dödas
den bakgrundsprocess som utför transkriberingen på riktigt (se
`modules/transcription_worker.py`), så CPU/GPU frigörs direkt istället
för att fortsätta osynligt i bakgrunden. En ny sådan process startas
automatiskt åt nästa jobb i kön - vid lokal Whisper laddas modellen då om
(några sekunders extra fördröjning just då, annars ingen skillnad). I
övriga steg (klippning, AI-berikning) avbryts jobbet så snart det
pågående steget är klart. Efter att avsnittet publicerats på Spreaker går
det inte längre att avbryta (kan inte ångras).

En avbruten predikans originalfil i `uploads/` lämnas orörd (det kan vara
din enda kopia av ljudet) - bara de ofärdiga resultatfilerna i
`processed/` städas bort.

**Hantera enskilda rader i kön:**

- **"⬆ Prioritera"** (visas på väntande rader) flyttar raden längst fram
  i kön, så den bearbetas härnäst - t.ex. praktiskt om en predikan
  behöver publiceras skyndsamt bland flera väntande.
- **"✕ Ta bort"** (visas på alla rader utom den som bearbetas just nu)
  tar bort en enskild rad ur kön/listan. Vill du ta bort en rad som
  bearbetas just nu, avbryt den först (🚫 Avbryt).
- **"🧹 Rensa fel/avbrutna"** tar bort alla rader med status Fel eller
  Avbruten på en gång - t.ex. praktiskt efter en CSV-bulkimport där vissa
  rader misslyckades med "filen hittades inte" (redan importerade
  tidigare, se avsnitt 11).
- **"🗑️ Rensa allt"** tömmer hela kön (efter en bekräftelsedialog) -
  väntande, klara, misslyckade och avbrutna rader. En rad som bearbetas
  just nu påverkas aldrig av detta, den fortsätter tills den blir klar
  eller avbryts separat.

Att ta bort en rad ur kön påverkar bara själva kö-listan/vyn - för en rad
som redan bearbetats klart (eller misslyckats/avbrutits) har filhanteringen
redan skett (se ovan); att ta bort raden här är bara städning av listan.

Kön nås även direkt via `GET /api/queue`, `POST /api/queue/pause`,
`POST /api/queue/resume`, `POST /api/queue/cancel/{job_id}`,
`DELETE /api/queue/{queue_id}`, `POST /api/queue/clear-errors`,
`POST /api/queue/clear` och `POST /api/queue/prioritize/{queue_id}`.

## 7. Vanliga frågor / felsökning

**`ModuleNotFoundError: No module named 'pkg_resources'` vid `pip install -r requirements.txt`**
→ `openai-whisper` behöver `setuptools` för att byggas, vilket inte alltid
  följer med automatiskt i nyare Python-versioners venv. Uppdatera
  setuptools i din venv och försök igen:
  ```powershell
  venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
  venv\Scripts\python.exe -m pip install -r requirements.txt
  ```
  Krånglar det ändå (vanligt på Python 3.12/3.13, där `openai-whisper` är
  dåligt underhållet) finns två alternativ:
  1. **Byt till `faster-whisper`** - snabbare och bättre underhållen. Ta
     bort/kommentera `openai-whisper==20231117` i `requirements.txt` och
     avkommentera `faster-whisper==1.0.3` istället.
  2. **Kör molnbaserat istället** - sätt `USE_LOCAL_WHISPER=false` i
     `.env` (kräver `OPENAI_API_KEY`), då behövs `openai-whisper` inte
     alls - se "Molnbaserat läge" ovan.

**"Kunde inte läsa ljudfilen" vid uppladdning**
→ Kontrollera att ffmpeg är installerat (`ffmpeg -version`).

**"OPENAI_API_KEY saknas"**
→ Lägg till nyckeln i `.env`, eller kör helt offline istället: sätt
  `USE_LOCAL_WHISPER=true` (transkribering) och `AI_PROVIDER=ollama`
  (titel/beskrivning) - se avsnittet "Helt offline-läge" ovan.

**"Kunde inte ansluta till Ollama"**
→ Kontrollera att Ollama-appen körs (den brukar synas i aktivitetsfältet),
  eller starta den manuellt med `ollama serve`. Kontrollera också att
  modellen är nedladdad: `ollama pull llama3.1` (eller vilken modell du
  angett i `OLLAMA_MODEL`).

**Spreaker-publiceringen är "simulerad"**
→ Det betyder att `SPREAKER_SIMULATE=true` eller att token/show-id saknas i
  `.env`. Fyll i riktiga uppgifter och sätt `SPREAKER_SIMULATE=false` för att
  publicera på riktigt.

**Långa predikor tar tid**
→ Bearbetningen körs i bearbetningskön och sidan pollar statusen där var
  1,5 sekund, så du ser exakt vilket steg som pågår just nu (⚙️ pågår,
  ✅ klart, ⏭️ hoppades över, ❌ fel). En 45-minuters predikan kan ändå ta
  någon minut totalt, särskilt med lokal Whisper/Ollama på en vanlig dator.

## 8. Databas

Bearbetningskön och episodhistoriken (som statistiken och
lagringsrensningen bygger på) sparas i en SQLite-databas (`predikan.db` i
projektroten som standard, styrs av `DATABASE_FILE`) - se `modules/db.py`,
`modules/queue_store.py` och `modules/episode_store.py`. Ingen separat
databasserver behövs; SQLite är inbyggt i Python och passar en lokal
enanvändarapp precis som den här.

Det praktiska värdet: **bearbetningskön överlever en omstart av servern**
(t.ex. när du uppdaterar koden, eller datorn startas om) - inklusive
pausläget och alla väntande predikningar du lagt dit. Ett jobb som
faktiskt höll på att bearbetas när servern stannade kan förstås inte
återupptas mitt i (det verkliga arbetet dog med processen) - det markeras
istället tydligt som "Fel" med ett förklarande meddelande nästa gång
servern startar, så du ser vad som hände och kan lägga till det igen om
du vill.

Live per-steg-procent under en pågående bearbetning sparas medvetet
**inte** i databasen (bara i minnet) - det är ren animation i
gränssnittet, inte data som behöver överleva en omstart.

## 9. Prestandastatistik & tidsuppskattning

Varje lyckad bearbetning sparas som en rad i databasen (avsnitt 8): total
bearbetningstid, predikans längd, titel, taggar, länk m.m. Statistiken
(`GET /api/stats`) beräknas som en aggregatfråga över dessa rader: totalt
antal, total bearbetningstid, total predikantid, och kvoten mellan dem
("processing_ratio" - bearbetningssekunder per sekund predikan) som
används för att uppskatta hur lång tid nästa predikan tar - t.ex. om en
38-minuters predikan hittills tagit i snitt 20 minuter att bearbeta,
uppskattas en 19-minuters predikan ta ca 10 minuter på samma dator.

Statistiken visas överst på sidan, och en uppskattad bearbetningstid för
det just nu valda klippet visas under vågformen (uppdateras live när du
justerar start-/slutpunkten). Innan någon predikan bearbetats finns ingen
historik än, så ingen uppskattning visas.

## 10. Begränsa lagringsutrymme (`MAX_STORED_EPISODES`)

`uploads/` och `processed/` växer annars oändligt vid drift över lång tid,
eftersom varje bearbetad predikan lämnar kvar originalfilen samt klippt
ljud, transkript och AI-berikning. Sätt `MAX_STORED_EPISODES` i `.env` till
ett heltal för att bara behålla de senaste N predikningarna - äldre städas
bort automatiskt (både i `uploads/` och `processed/`, och deras rad i
databasen) direkt efter varje lyckad bearbetning. Vilka episoder som är
"äldst" avgörs av databasen (avsnitt 8), inte genom att tolka filnamn i
mapparna. Lämna tomt/`0` (standard) för ingen begränsning.

Filer som laddats upp men ännu inte bearbetats klart rörs aldrig av
städningen.

## 11. Bulk-importera predikningar via CSV

För att importera flera predikningar på en gång (t.ex. ett arkiv av äldre
inspelningar):

1. Lägg ljudfilerna (samma format som stöds vid vanlig uppladdning, se
   avsnitt 5) i mappen som `BULK_IMPORT_DIR` pekar på (standard:
   `bulk_import/` i projektroten).
2. Skapa en CSV-fil med följande kolumner (case-insensitive, svenska eller
   engelska namn fungerar båda):

   | Kolumn                    | Obligatorisk | Exempel        |
   |----------------------------|:---:|----------------|
   | `filnamn` / `filename`     | Ja  | `2024-03-10.mp3` |
   | `talare` / `speaker`       | Ja  | `Pastor Anna Andersson` |
   | `datum` / `date`           | Ja  | `2024-03-10` (ÅÅÅÅ-MM-DD) |
   | `klockslag` / `time`       | Ja  | `11:00` (TT:MM) |
   | `titel` / `title`          | Nej | Lämna tomt för AI-genererad titel |

3. Ladda upp CSV-filen i sektionen **"📥 Bulk-importera predikningar (CSV)"**
   längst ner på sidan (eller `POST /api/bulk-import`).

CSV-filens STRUKTUR (kolumner, datum/klockslag-format, obligatoriska fält)
valideras innan något börjar bearbetas - om en rad har fel där (t.ex.
ogiltigt datum) avbryts hela importen med en tydlig felbeskrivning, så att
inget hinner bearbetas halvvägs. Varje predikan bearbetas i sin helhet
(ingen manuell klippning i vågformen) och publiceras/schemaläggs enligt
kombinationen av datum och klockslag - precis som `publish_date` i det
vanliga flödet (se avsnitt 5). Raderna läggs till i samma bearbetningskö
som manuellt klippta predikningar (se avsnitt 6), i CSV-filens ordning -
framstegen för varje rad följs där.

**Ljudfilen tas bort från `BULK_IMPORT_DIR` bara vid lyckad publicering.**
Misslyckas en rad (t.ex. fel från Spreaker) lämnas ljudfilen orörd kvar,
och alla halvfärdiga filer den hunnit skapa i `uploads/`/`processed/`
städas bort. Det gör att du kan **köra om samma CSV-fil** efter att ha
rättat ett fel (eller efter ett tillfälligt nätverksproblem) - rader som
redan lyckats ger då bara ett harmlöst "filen hittades inte"-fel (filen är
redan importerad och borttagen), medan resten av raderna bearbetas som
vanligt.

## 12. Loggning

Allt som händer under en CSV-bulkimport skrivs till en loggfil (`app.log`
i projektroten som standard, styrs av `LOG_FILE`) - vilken fil som
bearbetas, lyckad publicering (och borttagning från `BULK_IMPORT_DIR`),
eller varför en rad misslyckades. Loggnivån styrs av `LOG_LEVEL` i `.env`
(`DEBUG`, `INFO`, `WARNING`, `ERROR` eller `CRITICAL` - standard `INFO`).
Sätt t.ex. `LOG_LEVEL=ERROR` för att bara logga faktiska fel.

## 13. Utveckling

Installera utvecklingsberoenden (utöver `requirements.txt`):

```bash
pip install -r requirements-dev.txt
```

**Tester** (`tests/`, pytest mot en temporär databas/temporära kataloger -
rör aldrig din riktiga `predikan.db`/`uploads/`/`processed/`/`podcast_arkiv/`):

```bash
pytest
```

**Lint och typkontroll** (konfiguration i `pyproject.toml`):

```bash
ruff check .
mypy .
```

## 14. Nästa steg (idéer för v2)

- Stöd för fler podcast-plattformar (Acast, Apple Podcasts via RSS, etc.)
- Bläddringsbar historik/lista över tidigare publicerade avsnitt i
  gränssnittet (episodhistoriken finns redan i databasen, se avsnitt 8 -
  bara ingen vy för att bläddra i den än)
- Inloggning/multianvändarstöd
- Automatisk paus-/tystnadsdetektering för att föreslå klippunkter

## 15. Köra i Docker

Ett alternativ till venv: `Dockerfile` + `docker-compose.yml` finns i
projektroten och paketerar hela appen (inklusive ffmpeg) i en image.

```bash
cp .env-example .env    # om du inte redan har en .env, se avsnitt 3
docker compose up --build
```

Öppna sedan **http://127.0.0.1:8000** precis som vanligt.

**Data som sparas mellan omstarter** (monteras som volymer i
`docker-compose.yml`): `uploads/`, `processed/`, `bulk_import/`,
`podcast_arkiv/` samt en `data/`-mapp som innehåller SQLite-databasen och
loggfilen (kompositionen omdirigerar `DATABASE_FILE`/`LOG_FILE` dit istället
för till projektroten, se avsnitt 8 och 12).

**Podd-arkivet på en annan disk i Docker:** ändra *vänster* sida av
volymen i `docker-compose.yml`, t.ex. `D:/Podcastarkiv:/app/podcast_arkiv`.
Låt `ARCHIVE_DIR` vara kvar som den är - den anger sökvägen *inuti*
containern.

**Ollama från en container**: om `AI_PROVIDER=ollama` och Ollama körs på
värddatorn (inte i en egen container) räcker det inte med
`OLLAMA_HOST=http://localhost:11434` i `.env` - `localhost` pekar då på
containern själv. Sätt istället `OLLAMA_HOST=http://host.docker.internal:11434`.

**Nu**: bygget är CPU-only, vilket matchar `USE_LOCAL_WHISPER=true` med
lokal Whisper på CPU (se avsnitt 3). Applikationskoden kräver INGA
ändringar för att senare köra på GPU - enhetsvalet
(`modules/transcription.py:_resolve_device`) sker redan automatiskt vid
körning via `WHISPER_DEVICE`. Det som krävs för GPU-stöd i Docker senare:

1. NVIDIA-drivrutin + [NVIDIA Container
   Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   installerat på värden.
2. Byt basimage i `Dockerfile` från `python:3.11-slim` till en med
   CUDA-runtime, t.ex. `nvidia/cuda:12.1.0-runtime-ubuntu22.04` (kräver
   att python/pip installeras separat i den imagen).
3. Avkommentera `deploy.resources.reservations.devices`-blocket i
   `docker-compose.yml`.
4. Sätt `WHISPER_DEVICE=cuda` i `.env` (eller lämna `auto`).

## 16. Hantera Spreaker

Fliken **📡 Hantera Spreaker** visar avsnitten som redan ligger på ditt
Spreaker-konto - oavsett om de publicerades via appen eller på annat sätt -
och låter dig redigera titel och beskrivning. Avsnittslistan visas när
`SPREAKER_API_TOKEN` och `SPREAKER_SHOW_ID` är ifyllda och
`SPREAKER_SIMULATE=false`. (Podd-arkivet i samma flik kräver bara
`SPREAKER_SHOW_ID`, se avsnitt 17.)

- **🔄 Hämta från Spreaker** hämtar en färsk lista. Listan sparas lokalt i
  databasen, så fliken öppnas snabbt utan nya anrop till Spreaker.
- **Sortering och sidor:** klicka på en kolumnrubrik för att sortera.
  Listan visas sida för sida; antal per sida (10/25/50/100/alla) väljs under
  tabellen och kommer ihåg sig i webbläsaren.
- **Redigera** titel och beskrivning direkt i tabellen. "Talare" läses ut ur
  beskrivningens sista `Talare: ...`-rad.
- **Spara:** **💾 Spara** på en rad sparar bara den raden till Spreaker.
  **💾 Spara ändringar** överst sparar alla ändrade rader på en gång.
- **Arkiv-kolumnen** visar 🗄️ om avsnittets ljud finns i det lokala
  podd-arkivet och 📝 om ett transkript finns sparat (se avsnitt 17).

**🤖 Generera om titel eller beskrivning med AI:** klicka 🤖 Titel eller
🤖 Beskrivning på en rad. Jobbet läggs i bearbetningskön (avsnitt 6):

1. Finns ett sparat transkript (📝) återanvänds det. Annars transkriberas
   ljudet - från det lokala arkivet om det finns där (🗄️), annars laddas det
   ner från Spreaker. Kryssa i **Transkribera om** för att tvinga fram en ny
   transkribering.
2. AI:n skriver ett förslag med prompten från inställningarna (se "Egna
   AI-prompter" i avsnitt 3).
3. Förslaget visas **bredvid den nuvarande versionen**. Redigera det om du
   vill, klicka **↩️ Behåll nuvarande** för att slänga förslaget, eller
   **💾 Spara** för att skicka det till Spreaker.

Ingenting skrivs till Spreaker förrän du själv klickar Spara.

## 17. Lokalt podd-arkiv

Längst ner i fliken **📡 Hantera Spreaker** finns **🗄️ Lokalt podd-arkiv**,
som sparar en lokal kopia av hela podden. Klicka **⬇️ Arkivera podden** så
laddas alla avsnitt ner från poddens publika RSS-flöde hos Spreaker. Det
kräver bara `SPREAKER_SHOW_ID` - ingen token. Framstegen visas medan det
pågår, och körningen kan avbrytas med **⏹️ Avbryt**.

Per avsnitt sparas:

| Fil                | Innehåll |
|--------------------|----------|
| `.mp3`             | Ljudet |
| `.xml`             | Avsnittets originaldata ur RSS-flödet (skrivs om varje körning så ändringar följer med) |
| `.txt`             | Läsbar sammanställning: titel, talare, datum, längd, nyckelord, länkar och beskrivning |
| `.transkript.txt`  | Hela transkriberingen - när avsnittet transkriberats via "Generera om" (avsnitt 16) |

En logg över varje körning sparas i `logg.txt` i arkivmappen. Filerna
namnges `ÅÅÅÅ-MM-DD_TT-MM_Talare_Titel` (lokal tid).

**Kör gärna om arkiveringen då och då.** Avsnitt vars mp3 redan finns laddas
inte ner igen - bara nya avsnitt hämtas, och `.xml`/`.txt` uppdateras.
Transkript som redan finns i databasen följer med till arkivet vid nästa
körning.

**Arkivmappen** är `podcast_arkiv/` i projektmappen som standard. Byt den
under **⚙️ Inställningar → 🗄️ Lagring & loggning** eller med `ARCHIVE_DIR`
i `.env`, t.ex. för att lägga arkivet på en annan disk. Mappen skapas först
när du arkiverar, så appen startar även om en extern disk är urkopplad -
arkiveringen ger då ett tydligt fel, och Arkiv-kolumnen i avsnittslistan
visar bara inga ikoner. (I Docker: se avsnitt 15.)
