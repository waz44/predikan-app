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
│   └── email_notifier.py     # Bekräftelsemail
├── static/
│   ├── index.html            # Frontend (uppladdning, vågform, formulär)
│   ├── style.css
│   └── app.js                 # Wavesurfer.js-integration + API-anrop
├── uploads/                   # Original-filer (skapas automatiskt)
└── processed/                 # Klippta/färdiga filer (skapas automatiskt)
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
   transkriberingen. Fyll i **Publiceringsdatum** om du vill schemalägga
   avsnittet till en framtida tidpunkt - lämna tomt för att publicera direkt.
4. Klicka **"Klipp, transkribera & publicera"**. Applikationen:
   - klipper och volymnormaliserar ljudet
   - transkriberar det till text
   - genererar titel/beskrivning/taggar med GPT (endast för tomma fält)
   - laddar upp och publicerar (eller schemalägger) avsnittet på Spreaker
   - skickar bekräftelse (e-post eller sammanfattningssida)

   Under bearbetningen ser du en **procentmätare per steg** (samt en
   sammanvägd totalprocent högst upp). Spreaker-uppladdningen visar verklig,
   exakt procent baserat på hur mycket av filen som skickats. Övriga steg
   (transkribering, AI-berikning m.m.) visar en uppskattad procent baserat på
   ljudlängd och en tumregel för hastighet, eftersom de biblioteken inte
   rapporterar exakt framdrift internt.
5. Du får en **sammanfattning** med titel, talare, beskrivning, taggar,
   publiceringstid och länk till det publicerade avsnittet.

## 6. Vanliga frågor / felsökning

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
→ Bearbetningen körs som ett bakgrundsjobb och sidan pollar statusen varje
  sekund, så du ser exakt vilket steg som pågår just nu (⚙️ pågår, ✅ klart,
  ⏭️ hoppades över, ❌ fel). En 45-minuters predikan kan ändå ta någon minut
  totalt, särskilt med lokal Whisper/Ollama på en vanlig dator.

## 7. Nästa steg (idéer för v2)

- Bakgrundsjobb (Celery/RQ) + progress-bar istället för synkron bearbetning
- Stöd för fler podcast-plattformar (Acast, Apple Podcasts via RSS, etc.)
- Historik/lista över tidigare publicerade avsnitt
- Inloggning/multianvändarstöd
- Automatisk paus-/tystnadsdetektering för att föreslå klippunkter
