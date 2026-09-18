# Predikan → Podcast

Lokal webbapplikation för att klippa, transkribera, AI-berika och publicera
predikor till Spreaker.

## Arkitektur

```
predikan-app/
├── app.py                    # FastAPI-huvudapplikation (alla API-endpoints)
├── config.py                 # Läser in .env
├── requirements.txt
├── .env.example               # Mall för dina API-nycklar (kopiera till .env)
├── modules/
│   ├── audio_processor.py    # Klippning + normalisering (pydub/ffmpeg)
│   ├── transcription.py      # Whisper (OpenAI API eller lokalt)
│   ├── ai_enrichment.py      # GPT: titel/beskrivning/taggar
│   ├── spreaker_client.py    # Spreaker API-uppladdning (+ simuleringsläge)
│   ├── email_notifier.py     # Bekräftelsemail
│   ├── stats.py              # Prestandastatistik (se avsnitt 8)
│   ├── storage_cleanup.py    # Begränsning av uploads/+processed/ (se avsnitt 9)
│   └── app_logging.py        # Loggkonfiguration (se avsnitt 11)
├── static/
│   ├── index.html            # Frontend (uppladdning, vågform, formulär)
│   ├── style.css
│   └── app.js                 # Wavesurfer.js-integration + API-anrop
├── uploads/                   # Original-filer (skapas automatiskt)
├── processed/                 # Klippta/färdiga filer (skapas automatiskt)
├── bulk_import/                # Ljudfiler för CSV-bulkimport (se avsnitt 10)
├── stats.json                 # Ackumulerad prestandastatistik (skapas automatiskt)
└── app.log                    # Loggfil (skapas automatiskt, se avsnitt 11)
```

## 1. Förutsättningar

- **Python 3.10+**
- **ffmpeg** installerat och tillgängligt i PATH (krävs av pydub för att läsa/skriva mp3/wav):
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: ladda ner från https://ffmpeg.org/download.html och lägg till i PATH

Kontrollera installationen:
```bash
ffmpeg -version
```

## 2. Installation

```bash
cd predikan-app
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Konfiguration

```bash
cp .env.example .env
```

Öppna `.env` och fyll i:

### Helt offline-läge (rekommenderas om du vill slippa OpenAI helt)

Standardvärdena i `.env.example` är redan inställda för offline-drift:
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
  sparas i `uploads/` + `processed/` samtidigt. Se avsnitt 9 nedan.
- **BULK_IMPORT_DIR** (valfritt, standard `bulk_import/`) – mapp där
  ljudfiler för CSV-bulkimport ska ligga. Se avsnitt 10 nedan.
- **LOG_LEVEL** / **LOG_FILE** (valfritt) – styr loggfilen (`app.log` som
  standard). Se avsnitt 11 nedan.

### Så här skaffar du Spreaker-uppgifter (SPREAKER_API_TOKEN + SPREAKER_SHOW_ID)

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

Öppna sedan: **http://127.0.0.1:8000**

## 5. Användarflöde

1. **Ladda upp** en `.mp3`- eller `.wav`-fil.
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

## 6. Bearbetningskö (pausa/starta)

Alla predikningar - både manuellt klippta och rader från CSV-bulkimport
(avsnitt 10) - hamnar i **samma bearbetningskö**, i den ordning de lades
till. Kön bearbetar bara **en predikan i taget**, så aldrig mer än en tung
Whisper/Ollama/Spreaker-körning pågår samtidigt - det är själva poängen,
för att kunna styra hur mycket av datorns resurser bearbetningen tar vid
lokal körning.

Kolumnen till höger visar kön live (pollas var 1,5 sekund) med status,
procent och (för den som bearbetas just nu) samma detaljerade stegvy som
tidigare. Knappen **"⏸ Pausa" / "▶ Starta"** styr om kön ska plocka upp
nästa väntande predikan:

- **Pausad:** inget nytt objekt påbörjas, men en predikan som redan
  påbörjats slutförs alltid (den kan inte avbrytas säkert mitt i). Praktiskt
  för att i lugn och ro klippa och lägga till flera predikningar utan att
  belasta datorn förrän du är redo - klicka sedan "Starta" för att bearbeta
  hela kön i ett svep.
- **Kör** (standard): nästa väntande predikan i kön påbörjas så snart
  föregående är klar.

Kön nås även direkt via `GET /api/queue`, `POST /api/queue/pause` och
`POST /api/queue/resume`.

## 7. Vanliga frågor / felsökning

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

## 8. Prestandastatistik & tidsuppskattning

Varje lyckad bearbetning loggas till `stats.json` (skapas automatiskt i
projektroten): total bearbetningstid, total predikantid och antal
bearbetade predikningar. Kvoten mellan dem ("processing_ratio" -
bearbetningssekunder per sekund predikan) används för att uppskatta hur
lång tid nästa predikan tar - t.ex. om en 38-minuters predikan hittills
tagit i snitt 20 minuter att bearbeta, uppskattas en 19-minuters predikan
ta ca 10 minuter på samma dator.

Statistiken visas överst på sidan, och en uppskattad bearbetningstid för
det just nu valda klippet visas under vågformen (uppdateras live när du
justerar start-/slutpunkten). Statistiken nås även direkt via
`GET /api/stats`. Innan någon predikan bearbetats finns ingen historik än,
så ingen uppskattning visas.

## 9. Begränsa lagringsutrymme (`MAX_STORED_EPISODES`)

`uploads/` och `processed/` växer annars oändligt vid drift över lång tid,
eftersom varje bearbetad predikan lämnar kvar originalfilen samt klippt
ljud, transkript och AI-berikning. Sätt `MAX_STORED_EPISODES` i `.env` till
ett heltal för att bara behålla de senaste N predikningarna - äldre städas
bort automatiskt (både i `uploads/` och `processed/`) direkt efter varje
lyckad bearbetning. Lämna tomt/`0` (standard) för ingen begränsning.

Filer som laddats upp men ännu inte bearbetats klart rörs aldrig av
städningen.

## 10. Bulk-importera predikningar via CSV

För att importera flera predikningar på en gång (t.ex. ett arkiv av äldre
inspelningar):

1. Lägg ljudfilerna (`.mp3`/`.wav`) i mappen som `BULK_IMPORT_DIR` pekar på
   (standard: `bulk_import/` i projektroten).
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

## 11. Loggning

Allt som händer under en CSV-bulkimport skrivs till en loggfil (`app.log`
i projektroten som standard, styrs av `LOG_FILE`) - vilken fil som
bearbetas, lyckad publicering (och borttagning från `BULK_IMPORT_DIR`),
eller varför en rad misslyckades. Loggnivån styrs av `LOG_LEVEL` i `.env`
(`DEBUG`, `INFO`, `WARNING`, `ERROR` eller `CRITICAL` - standard `INFO`).
Sätt t.ex. `LOG_LEVEL=ERROR` för att bara logga faktiska fel.

## 12. Nästa steg (idéer för v2)

- Stöd för fler podcast-plattformar (Acast, Apple Podcasts via RSS, etc.)
- Historik/lista över tidigare publicerade avsnitt
- Inloggning/multianvändarstöd
- Automatisk paus-/tystnadsdetektering för att föreslå klippunkter
