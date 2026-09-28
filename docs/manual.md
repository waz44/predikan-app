# Användarmanual – Predikan → Podcast

Den här manualen är för dig som ska installera och använda appen, utan att
behöva kunna programmering. Den tekniska dokumentationen finns i
[README.md](../README.md).

**Innehåll**

1. [Vad appen gör](#1-vad-appen-gör)
2. [Det här behöver du](#2-det-här-behöver-du)
3. [Installera på en Windows-dator](#3-installera-på-en-windows-dator)
4. [Första inställningarna](#4-första-inställningarna)
5. [Publicera en predikan](#5-publicera-en-predikan)
6. [Bearbetningskön](#6-bearbetningskön)
7. [Hantera avsnitt som redan ligger på Spreaker](#7-hantera-avsnitt-som-redan-ligger-på-spreaker)
8. [Lokalt podd-arkiv](#8-lokalt-podd-arkiv)
9. [Importera många predikningar på en gång](#9-importera-många-predikningar-på-en-gång)
10. [Använda appen från en annan dator](#10-använda-appen-från-en-annan-dator)
11. [Uppdatera till en ny version](#11-uppdatera-till-en-ny-version)
12. [Säkerhetskopiera](#12-säkerhetskopiera)
13. [Köra allt på den egna datorn](#13-köra-allt-på-den-egna-datorn)
14. [Ta bort appen](#14-ta-bort-appen)
15. [Om något går fel](#15-om-något-går-fel)

---

## 1. Vad appen gör

Du laddar upp inspelningen från gudstjänsten, markerar var predikan börjar
och slutar, och skriver vem som talade. Sedan sköter appen resten:

1. **Klipper** ut predikan och jämnar ut ljudnivån.
2. **Skriver ut** allt som sägs som text (transkribering).
3. **Föreslår titel, beskrivning och taggar** med AI utifrån texten – om du
   inte skrivit dem själv.
4. **Publicerar** avsnittet i er podd på Spreaker – direkt, vid en senare
   tidpunkt, eller bakåtdaterat.

Appen körs på en dator hos er och används i webbläsaren. Den behöver inte
vara kraftfull: transkriberingen och AI-texterna görs som standard av
**gratistjänster på nätet** (Groq och Google Gemini).

## 2. Det här behöver du

- **En dator med Windows 10 eller 11** som får stå på. Appen körs i
  bakgrunden och startar av sig själv när datorn startar.
- **Internet.**
- **Administratörsbehörighet** på datorn under installationen.
- **Ett Spreaker-konto** med er podd.
- **Två gratis nycklar** – en från Groq och en från Google. Du skapar dem i
  [avsnitt 4](#4-första-inställningarna); det tar några minuter.

Räkna med ungefär en halvtimme första gången.

## 3. Installera på en Windows-dator

1. Öppna sidan med den senaste versionen:
   https://github.com/waz44/predikan-app/releases/latest
2. Under **Assets**, klicka på **`predikan-app-<version>-windows.zip`** för
   att ladda ner den.
3. Högerklicka på zip-filen → *Extrahera alla…*, skriv **`C:\`** som mål
   och klicka *Extrahera*. Allt hamnar då i mappen **`C:\PredikanApp`**.
   Lägg den **inte** under *Program Files* – appen sparar sina filer i sin
   egen mapp.
4. Öppna mappen och dubbelklicka på **`Installera.cmd`**.
5. Windows frågar om appen får göra ändringar – svara **Ja**.
6. Ett PowerShell-fönster visar vad som händer. Saknas Python eller ffmpeg (två
   program som appen behöver) frågar installationen om den får installera
   dem – tryck **Enter** för ja. Första installationen tar några minuter.
7. När det står **Klart!** öppnas appen i webbläsaren på
   **http://127.0.0.1:8000**. Tryck Enter för att stänga PowerShell-fönstret.

Appen körs nu som tjänsten **Predikan → Podcast** och startar automatiskt
när datorn startar – ingen behöver vara inloggad. Spara adressen
http://127.0.0.1:8000 som bokmärke.

> **Visar Windows en säkerhetsvarning för `Installera.cmd`?** Det beror på
> att filen kommer från internet. Välj *Kör* (eller *Mer information → Kör
> ändå*). Filerna kommer direkt från appens sida på GitHub.

## 4. Första inställningarna

Öppna fliken **⚙️ Inställningar** i appen.

### 4.1 Groq – för transkribering (gratis)

1. Gå till https://console.groq.com/keys och skapa ett konto (det går att
   logga in med ett Google-konto).
2. Klicka **Create API Key**, ge nyckeln ett namn (t.ex. "Predikan") och
   kopiera den. Den börjar med `gsk_`.
3. I appen, under **🆓 Gratistjänster på nätet**: klistra in nyckeln i
   **Groq-nyckel** och klicka **Verifiera nyckel**.

### 4.2 Gemini – för titel, beskrivning och taggar (gratis)

1. Gå till https://aistudio.google.com/apikey och logga in med ett
   Google-konto.
2. Klicka **Create API key** och kopiera nyckeln. Den börjar med `AIza`.
3. I appen: klistra in den i **Gemini-nyckel** och klicka **Verifiera nyckel**.

> På Googles gratisnivå får Google använda det som skickas för att förbättra
> sina tjänster. Predikningarna publiceras ändå offentligt, men det är bra
> att känna till.

### 4.3 Välj tjänsterna och spara

1. Under **📝 Transkribering**: välj **Groq – Whisper large-v3** i
   *Transkribera med*.
2. Under **🤖 AI-berikning**: välj **gemini** i *Provider*.
3. Klicka **💾 Spara alla inställningar** längst ner.

### 4.4 Spreaker – för att publicera

Under **📡 Spreaker** finns en guide i fyra steg. Du gör den en gång.

1. **Steg 1:** Logga in på Spreaker och öppna
   https://www.spreaker.com/account/developer/enable. Registrera en ny app
   (för eget bruk) och ange `http://localhost` som *Redirect URI*. Kopiera
   **Client ID** och **Client Secret** till fälten i appen.
2. **Steg 2:** Klicka **🔗 Steg 2: Skapa auktoriseringslänk** och öppna
   länken som visas. Logga in och klicka **Tillåt**.
3. **Steg 3:** Du hamnar på en sida som inte fungerar – det är meningen.
   Kopiera hela adressen i webbläsarens adressfält, klistra in den i fältet
   *Steg 3* och klicka **🔑 Byt kod mot token**.
4. **Steg 4:** Välj er podd i listan.
5. Låt **Simulera publicering** vara ikryssad tills du provat en gång (se
   nedan) och klicka **💾 Spara Spreaker-inställningar**.

> **Simulera publicering:** så länge rutan är ikryssad skickas ingenting
> till Spreaker. Allt annat görs på riktigt, så du kan prova hela flödet
> ofarligt. Bocka ur rutan och spara när du vill publicera på riktigt.

### 4.5 E-post (valfritt)

Vill du få ett mejl när ett avsnitt är publicerat, fyll i
**📧 E-postbekräftelse** med uppgifter från er e-postleverantör och spara.
Annars syns resultatet i appen.

## 5. Publicera en predikan

Fliken **🎙️ Bearbeta predikningar**.

### Steg 1 – Ladda upp

Välj ljudfilen från gudstjänsten. Vanliga format fungerar (MP3, WAV, M4A,
AAC, OGG, Opus, FLAC, WMA) och filen får vara upp till 4 GB – det räcker för
en inspelning på över två timmar i högsta kvalitet.

### Steg 2 – Klipp

- Spela upp med **▶ Start** och hoppa med **⏪ -15s** / **+15s ⏩**.
- Markera predikan genom att **dra i kanterna på den blå rutan** i
  vågformen – eller spela fram till rätt ställe och klicka
  **Sätt start = nuvarande tid** respektive **Sätt slut = nuvarande tid**.
- **🔊 Normalisera ljud** (valfritt): är inspelningen svag eller ojämn,
  klicka här först. Ljudnivån jämnas ut direkt, så det blir lättare att
  lyssna och hitta var predikan börjar. (Ljudet jämnas alltid ut när
  predikan klipps, även om du inte klickar här.)

När appen har bearbetat minst en predikan visas under vågformen en
uppskattning av hur lång tid bearbetningen tar.

### Steg 3 – Uppgifter om predikan

- **Talare** – måste fyllas i. Namnet skrivs sist i beskrivningen.
- **Titel** och **Beskrivning** – lämna tomma så skriver AI:n förslag
  utifrån predikan. Fyller du i dem används din text.
- **Publiceringsdatum** (valfritt):
  - tomt = publiceras direkt,
  - ett **framtida** datum = publiceras då,
  - **dagens** datum eller **tidigare** = avsnittet bakåtdateras (t.ex. för
    en äldre inspelning).

Klicka **➕ Lägg till i kö**. Formuläret töms direkt så att du kan ta nästa
predikan, medan den förra bearbetas i kön till höger.

## 6. Bearbetningskön

Kolumnen **🗂️ Bearbetningskön** till höger visar allt som ska göras. En
predikan i taget bearbetas, och du ser hur långt varje steg har kommit.
Transkriberingen görs i block om ungefär tio minuter, så där ser du till
exempel att block 3 av 6 är klart.

När en predikan är klar visas titeln, taggarna och en länk till avsnittet.

- **⏸ Pausa / ▶ Starta** – pausar kön efter den predikan som pågår.
  Praktiskt om du vill lägga in flera först.
- **🚫 Avbryt** – stoppar den predikan som bearbetas. Efter att den
  publicerats går det inte att avbryta.
- **⬆ Prioritera** – flyttar en väntande predikan först i kön.
- **✕ Ta bort** – tar bort en rad ur listan.
- **✅ Rensa klara**, **🧹 Rensa fel/avbrutna**, **🗑️ Rensa allt** – städar
  listan.

Kön finns kvar även om datorn startas om. En predikan som höll på att
bearbetas just då markeras som fel ("Bearbetningen avbröts av en omstart
av servern") – ladda upp och lägg till den igen.

## 7. Hantera avsnitt som redan ligger på Spreaker

Fliken **📡 Hantera Spreaker** syns när Spreaker är inställt och
*Simulera publicering* är urbockad.

- **🔄 Hämta från Spreaker** hämtar listan över poddens avsnitt.
- Ändra **titel** och **beskrivning** direkt i listan och klicka **💾 Spara**
  på raden, eller **💾 Spara ändringar** överst för alla ändrade rader.
- **🤖 Titel** / **🤖 Beskrivning** låter AI:n skriva ett nytt förslag. Det
  hamnar i kön och visas sedan bredvid den nuvarande texten. Ingenting
  ändras på Spreaker förrän du själv klickar **💾 Spara** – eller
  **↩️ Behåll nuvarande** för att slänga förslaget.

## 8. Lokalt podd-arkiv

Längst ner i **📡 Hantera Spreaker** finns **🗄️ Lokalt podd-arkiv**.
**⬇️ Arkivera podden** laddar ner alla avsnitt till datorn, med ljud,
beskrivning och (när det finns) transkript. Kör gärna om det då och då –
bara nya avsnitt hämtas.

Arkivet hamnar i mappen `podcast_arkiv` i appens mapp. Vill du ha det på en
annan disk, ändra **Mapp för podd-arkivet** under
**⚙️ Inställningar → 🗄️ Lagring & loggning**.

## 9. Importera många predikningar på en gång

För ett arkiv av äldre inspelningar finns **📥 Bulk-importera predikningar
(CSV)** längst ner på första fliken. Lägg ljudfilerna i mappen
`bulk_import` i appens mapp och ladda upp ett kalkylark (sparat som CSV) med
kolumnerna **filnamn**, **talare**, **datum** (ÅÅÅÅ-MM-DD), **klockslag**
(TT:MM) och valfritt **titel**. Mer om detta i
[README, avsnitt 11](../README.md#11-bulk-importera-predikningar-via-csv).

## 10. Använda appen från en annan dator

Som standard nås appen bara från datorn den är installerad på. Vill ni
kunna använda den från andra datorer i samma nätverk:

1. Öppna ett kommandofönster i appens mapp (skriv `cmd` i adressfältet i
   Utforskaren och tryck Enter).
2. Kör: `Installera.cmd -ListenOnNetwork`
3. Öppna sedan `http://DATORNAMN:8000` från de andra datorerna (installationen
   skriver ut det rätta namnet).

Fliken **⚙️ Inställningar** fungerar av säkerhetsskäl fortfarande bara på
själva datorn.

## 11. Uppdatera till en ny version

1. Kontrollera att ingen predikan bearbetas just nu.
2. Ladda ner den nya **`predikan-app-<version>-windows.zip`** från
   https://github.com/waz44/predikan-app/releases/latest
3. Packa upp den och kopiera innehållet i mappen `PredikanApp` **över** din
   befintliga mapp (t.ex. `C:\PredikanApp`). Välj *Ersätt filerna i målet*.
4. Dubbelklicka på **`Installera.cmd`** igen.

Dina inställningar, kön, ljudfilerna och arkivet finns kvar.
Vilken version som körs ser du på http://127.0.0.1:8000/api/version.

## 12. Säkerhetskopiera

Allt som är ert ligger i appens mapp. Det viktigaste att spara:

| Fil/mapp | Innehåll |
|---|---|
| `.env` | Alla inställningar och nycklar |
| `predikan.db` | Kön, historiken och sparade transkript |
| `podcast_arkiv` | Det lokala podd-arkivet (om ni använder det) |

`uploads` och `processed` innehåller originalfiler och färdiga filer för
predikningarna. De är bra att ha men kan bli stora.

## 13. Köra allt på den egna datorn

Gratistjänsterna räcker för några predikningar i veckan. Vill ni senare
köra allt utan tjänster på nätet går det att bygga ut:

- **Transkribering:** kör `Installera.cmd -WithLocalWhisper` och välj
  *Lokalt på den här datorn* under **📝 Transkribering**. Svensk
  KB-Whisper från Kungliga biblioteket ger bäst resultat. Utan grafikkort
  tar det från ungefär halva till hela predikans längd, beroende på modell.
- **Titel och beskrivning:** installera [Ollama](https://ollama.com/download),
  kör `ollama pull llama3.1` och välj **ollama** under **🤖 AI-berikning**.

Ett mellanläge: kryssa i **Gör jobbet lokalt om tjänsten på nätet inte
svarar** under **🆓 Gratistjänster på nätet**. Då används det lokala bara
när en gratistjänst inte svarar.

## 14. Ta bort appen

Dubbelklicka på **`Avinstallera.cmd`** i appens mapp. Tjänsten tas bort,
men mappen med inställningar och filer finns kvar – radera den själv om du
inte vill spara något.

## 15. Om något går fel

**Appen öppnas inte i webbläsaren**
- Vänta en halv minut efter att datorn startat – tjänsten startar lite
  fördröjt.
- Kontrollera att tjänsten körs: tryck Windows-tangenten, skriv
  *Tjänster*, öppna och leta upp **Predikan → Podcast**. Står den inte som
  *Körs*, högerklicka och välj *Starta*.
- Läs loggen `logs\service.log` i appens mapp – sista raderna brukar
  förklara felet.

**"GROQ_API_KEY saknas" eller "GEMINI_API_KEY saknas"**
Nyckeln är inte ifylld eller inte sparad. Se [avsnitt 4](#4-första-inställningarna).

**"Groq avvisade nyckeln" / "Google Gemini avvisade nyckeln"**
Nyckeln är felkopierad eller borttagen. Skapa en ny och klistra in den igen.

**Transkriberingen står still länge**
Gratisnivån hos Groq tar två timmar ljud per timme. Blir gränsen nådd väntar
appen den tid Groq anger och fortsätter sedan av sig själv.

**Avsnittet publicerades inte på Spreaker – det står "simulerad"**
*Simulera publicering* är ikryssad, eller så är Spreaker inte färdigt
inställt. Se [avsnitt 4.4](#44-spreaker--för-att-publicera).

**"Filen är för stor"**
Filen är större än 4 GB. Spara om inspelningen som MP3 eller i lägre
kvalitet.

**Något annat**
Loggfilerna `app.log` och `logs\service.log` i appens mapp beskriver vad
som hänt. Ta med dem om du ber någon om hjälp.
