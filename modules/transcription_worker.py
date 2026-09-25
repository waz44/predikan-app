"""
Modul: transcription_worker
Håller en långlivad bakgrundsprocess (transcription_worker_process.py -
se den filens docstring för varför den är en egen process istället för
att använda Pythons multiprocessing-modul) som utför den faktiska
transkriberingen, istället för att köra den i huvudprocessens
bearbetningstråd (samma tråd som hela bearbetningskön, se app.py).

Det gör att en pågående transkribering - den överlägset mest tidskrävande
delen av pipelinen, särskilt med lokal Whisper på CPU - går att avbryta på
riktigt genom att döda processen (CPU/GPU frigörs omedelbart), istället
för att bara markeras som avbruten medan beräkningen fortsätter osynligt
i bakgrunden.

Processen återanvänds mellan jobb, så en lokal Whisper-modell bara behöver
laddas en gång. Avbryts en transkribering dödas processen och en ny
startas automatiskt åt nästa jobb - då laddas modellen om (några sekunders
extra fördröjning just då), annars ingen skillnad mot att köra i samma
process.
"""
# json: förfrågan och svar skickas som en rad JSON i vardera riktningen.
import json

# queue: säker överlämning av svaret från lästråden till väntande kod.
# (Heter queue_module för att inte förväxlas med bearbetningskön.)
import queue as queue_module

# subprocess: startar och pratar med bakgrundsprocessen.
import subprocess

# sys.executable: sökvägen till Python-programmet som kör appen.
import sys

# threading: lås runt den delade processen och en tråd som läser svaret.
import threading

# Path: sökvägen till skriptet som bakgrundsprocessen kör.
from pathlib import Path

# config: transkriberingsinställningarna som skickas med varje förfrågan.
import config

# Skriptet som bakgrundsprocessen kör - ligger i samma mapp som den här filen.
_WORKER_SCRIPT = Path(__file__).resolve().parent / "transcription_worker_process.py"

# Låset skyddar _process, så att två trådar aldrig startar varsin process.
_lock = threading.Lock()
# Den körande bakgrundsprocessen, eller None innan den startats (eller efter
# att den dödats). Återanvänds mellan jobb så modellen bara laddas en gång.
_process: subprocess.Popen | None = None


class TranscriptionCancelled(Exception):
    """
    Kastas av transcribe() när cancel_event sätts under en pågående transkribering.

    services/pipeline.py fångar den och markerar jobbet som "Avbruten" i
    stället för "Fel".
    """


# Transkriberingsinställningarna skickas med VARJE förfrågan: bakgrunds-
# processen läser .env bara en gång när den startar, men inställnings-
# guiden ändrar config live i huvudprocessen (config.reload()). Då slår
# t.ex. ett byte till Pianissimo eller KB-Whisper igenom direkt, utan omstart.
SETTINGS_KEYS = (
    "USE_LOCAL_WHISPER",
    "LOCAL_WHISPER_MODEL",
    "WHISPER_DEVICE",
    "LOCAL_ASR_ENGINE",
    "PIANISSIMO_MODEL",
    "OPENAI_API_KEY",
)


def _current_settings() -> dict:
    """
    Transkriberingsinställningarna som de ser ut i huvudprocessen just nu.

    Returns:
        En dict med ett värde per nyckel i SETTINGS_KEYS, redo att skickas
        som JSON till bakgrundsprocessen.
    """
    return {key: getattr(config, key) for key in SETTINGS_KEYS}


def _spawn_worker(base_dir: Path) -> subprocess.Popen:
    """
    Startar en ny bakgrundsprocess för transkribering.

    Processen kör transcription_worker_process.py med SAMMA Python som appen
    (sys.executable), så den ser samma installerade paket.

    Args:
        base_dir: Appens mapp. Processen körs där, så att relativa sökvägar
            och .env hittas på samma sätt som i huvudprocessen.

    Returns:
        Den startade processen, med stdin/stdout som textströmmar.
    """
    return subprocess.Popen(
        [sys.executable, str(_WORKER_SCRIPT)],
        # stdin: förfrågningar dit. stdout: svar därifrån. stderr: modellens
        # loggutskrifter, som läses bara om processen dör (för felmeddelandet).
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Text i stället för bytes, alltid som UTF-8. Svaren är ren ASCII-JSON
        # (se transcription_worker_process.py), så inget kan tolkas fel här.
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # radbuffrat, så varje JSON-rad skickas/tas emot direkt
        cwd=str(base_dir),
    )


def _ensure_worker(base_dir: Path) -> subprocess.Popen:
    """
    Returnerar den körande bakgrundsprocessen - eller startar en ny om den saknas eller har dött.

    Args:
        base_dir: Appens mapp (skickas vidare till _spawn_worker).

    Returns:
        En levande process.
    """
    global _process
    with _lock:
        # poll() returnerar None medan processen lever, annars dess
        # avslutningskod - så "is not None" betyder att den har dött.
        if _process is None or _process.poll() is not None:
            _process = _spawn_worker(base_dir)
        return _process


def _kill_worker(process: subprocess.Popen) -> None:
    """
    Dödar en bakgrundsprocess (vid avbrott eller fel) och glömmer den.

    Nästa transkribering startar då automatiskt en ny process. Med lokal
    Whisper laddas modellen om, vilket tar några sekunder extra en gång.

    Args:
        process: Processen som ska dödas.
    """
    global _process
    try:
        # kill() avslutar processen direkt - CPU/GPU frigörs omedelbart, även
        # mitt i en transkribering.
        process.kill()
    except Exception:
        # Redan död - inget att göra.
        pass
    with _lock:
        # Glöm bara processen om det fortfarande är DEN som är registrerad;
        # en annan tråd kan redan ha startat en ny.
        if _process is process:
            _process = None


def transcribe(path: Path, base_dir: Path, cancel_event: threading.Event) -> str:
    """
    Skickar en transkriberingsförfrågan till bakgrundsprocessen och väntar
    på svaret - men avbryter (dödar processen) om cancel_event sätts under
    tiden, och kastar då TranscriptionCancelled. En ny process startas
    automatiskt vid nästa anrop.

    Args:
        path: Ljudfilen som ska transkriberas.
        base_dir: Appens mapp (där processen körs).
        cancel_event: Sätts när användaren klickar "Avbryt".

    Returns:
        Hela transkriptet som text.

    Raises:
        TranscriptionCancelled: Om cancel_event sattes.
        RuntimeError: Om processen dog eller transkriberingen misslyckades
            (t.ex. saknad modell eller OpenAI-nyckel).
    """
    process = _ensure_worker(base_dir)

    # Skicka förfrågan: EN rad JSON med ljudfilens sökväg och de aktuella
    # inställningarna. flush() ser till att den skickas direkt och inte
    # ligger kvar i en buffert.
    try:
        process.stdin.write(json.dumps({"path": str(path), "settings": _current_settings()}) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        # Processen har dött sedan förra jobbet - döda resterna och rapportera.
        _kill_worker(process)
        raise RuntimeError(f"Kunde inte skicka till transkriberingsprocessen: {exc}") from exc

    # Kön som lästråden lämnar svaret i. Plats för exakt ett svar.
    result_queue: queue_module.Queue[tuple[str, str]] = queue_module.Queue(maxsize=1)

    def _read_response() -> None:
        """
        Läser processens svar (en rad JSON) och lägger det i result_queue.

        Körs i en egen tråd, eftersom readline() blockerar tills svaret kommer
        - huvudloopen kan då under tiden kontrollera om användaren avbrutit.
        """
        try:
            line = process.stdout.readline()
        except Exception as exc:
            result_queue.put(("error", str(exc)))
            return
        result_queue.put(("line", line))

    threading.Thread(target=_read_response, daemon=True).start()

    # Vänta på svaret i korta intervall (0,3 s), så att ett klick på
    # "Avbryt" märks inom en tredjedels sekund även mitt i en lång
    # transkribering.
    while True:
        if cancel_event.is_set():
            # Döda processen - det är det enda sättet att stoppa Whisper mitt
            # i arbetet. En ny process startas automatiskt för nästa jobb.
            _kill_worker(process)
            raise TranscriptionCancelled("Transkriberingen avbröts av användaren.")
        try:
            kind, payload = result_queue.get(timeout=0.3)
            break
        except queue_module.Empty:
            continue

    # En tom rad betyder att processen stängde stdout, dvs. dog - t.ex. slut
    # på minne eller ett krasch i modellbiblioteket. Ta med slutet av dess
    # felutskrifter i meddelandet, så att orsaken syns i kön.
    if kind == "error" or not payload:
        stderr_tail = ""
        try:
            stderr_tail = (process.stderr.read(2000) or "").strip()
        except Exception:
            pass
        _kill_worker(process)
        detail = f" {stderr_tail}" if stderr_tail else ""
        raise RuntimeError(f"Transkriberingsprocessen avslutades oväntat.{detail}")

    # Svaret är {"ok": true, "transcript": "..."} eller {"ok": false, "error": "..."}.
    response = json.loads(payload)
    if not response.get("ok"):
        # Transkriberingen misslyckades men processen lever och kan återanvändas.
        raise RuntimeError(response.get("error") or "Okänt fel i transkriberingsprocessen.")
    return response["transcript"]
