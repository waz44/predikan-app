"""
Modul: services.state
Delat, rent processlokalt (icke-beständigt) runtime-tillstånd som både
routers/ och services/pipeline.py behöver komma åt. Allt här är avsiktligt
INTE sparat i databasen (se modules/queue_store.py för vad som faktiskt är
beständigt) - det är antingen kortlivat till sin natur (en uppladdning som
väntar på att klippas), eller meningslöst att återställa efter en omstart
(vad som pågår just nu, eller en avbrytningssignal för ett jobb vars
faktiska arbete redan skulle vara dött).
"""
import threading
from pathlib import Path

# Håller reda på uppladdade original-filer under körningens gång (file_id -> path).
# Kopplar bara ihop en uppladdning med vågforms-uppspelningen innan den
# eventuellt lagts i bearbetningskön.
UPLOADED_FILES: dict[str, Path] = {}

# Håller reda på avbrytningssignaler för jobb som just nu körs (job_id ->
# Event). Sätts av POST /api/queue/cancel/{job_id} och läses löpande av
# services/pipeline.py:_run_processing_job för att kunna avbryta på ett
# steg-boundary, samt av modules/transcription_worker.py för att döda den
# pågående transkriberingsprocessen på riktigt medan den arbetar.
CANCEL_EVENTS: dict[str, threading.Event] = {}

# Live per-steg-procent för det jobb som just nu bearbetas (job_id -> dict
# med "steps"/"overall_percent"/"estimated_seconds"). Uppdateras flera
# gånger per sekund under bearbetning - sparas MEDVETET inte till
# databasen eftersom det bara är UI-animation, inte data som behöver
# överleva en omstart.
RUNNING_PROGRESS: dict[str, dict] = {}

# Skyddar QUEUE_CURRENT_ID (nedan) mot samtidig åtkomst från kö-arbetartråden
# och request-hanterare.
STATE_LOCK = threading.Lock()

# Vilket köobjekt (queue_id) som just nu bearbetas, eller None. Rent
# efemärt - meningslöst efter en omstart eftersom inget hinner vara
# "pågående" precis vid start.
QUEUE_CURRENT_ID: str | None = None

# Signalerar till services/pipeline.py:queue_worker_loop att avsluta sin
# evighetsloop. Sätts av app.py:s shutdown-hook. Måste rensas (.clear())
# vid varje ny appstart - annars skulle en ny arbetartråd i samma process
# (t.ex. mellan pytest-tester som var och en startar/stänger appen) se
# eventet som redan satt och avsluta direkt utan att göra något.
WORKER_STOP_EVENT = threading.Event()


def set_current_queue_id(queue_id: str | None) -> None:
    global QUEUE_CURRENT_ID
    with STATE_LOCK:
        QUEUE_CURRENT_ID = queue_id


def get_current_queue_id() -> str | None:
    with STATE_LOCK:
        return QUEUE_CURRENT_ID
