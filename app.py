"""
Predikan-till-Podcast - huvudapplikation (FastAPI)

Kör lokalt med:
    uvicorn app:app --reload

Denna fil är bara app-sammansättning: skapar FastAPI-appen, kopplar in
routrarna (routers/), initierar databasen (modules/db.py) och startar den
enda bearbetningskö-arbetartråden (services/pipeline.py). Själva
endpoint-logiken bor i routers/, och bearbetningspipelinen i
services/pipeline.py.

Se README.md för fullständiga instruktioner.
"""
import threading

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from modules import app_logging, db, queue_store
from routers import bulk_import, process, queue, spreaker_episodes, stats, upload
from services.pipeline import queue_worker_loop

app = FastAPI(title="Predikan → Podcast")

app.include_router(upload.router)
app.include_router(process.router)
app.include_router(queue.router)
app.include_router(bulk_import.router)
app.include_router(stats.router)
app.include_router(spreaker_episodes.router)

_worker_thread: threading.Thread | None = None
_worker_stop_event: threading.Event | None = None


@app.on_event("startup")
def _on_startup() -> None:
    global _worker_thread, _worker_stop_event
    db.init_db()

    stale_count = queue_store.reset_stale_running("Bearbetningen avbröts av en omstart av servern.")
    if stale_count:
        app_logging.logger.warning(
            f"{stale_count} köobjekt var fortfarande markerade som pågående vid start - markerade som avbrutna."
        )

    # Startar den enda bearbetningsarbetaren för hela appens livstid.
    #
    # Både databasfilen och stoppsignalen fångas/skapas HÄR, i huvudtråden,
    # och skickas in som argument till tråden istället för att den läser
    # config.DATABASE_FILE eller en delad global Event själv när den kör
    # igång - threading.Thread.start() returnerar så fort tråden är
    # SCHEMALAGD, inte när den faktiskt fått köra sin första rad kod, och
    # under belastning kan de ligga sekunder isär (se
    # modules/db.py:bind_thread_to_database_file och
    # services/pipeline.py:queue_worker_loop för det fulla resonemanget).
    # En EGEN Event per arbetartråd (istället för en delad global som
    # rensas med .clear() vid varje ny appstart, som tidigare) gör
    # dessutom att bara denna trådens egen _on_shutdown någonsin kan
    # stoppa/återuppliva den - en delad global skulle kunna återuppliva en
    # föregående tests kvarlevande tråd (vars join() nedan hann ge upp
    # innan tråden faktiskt avslutat sig) när en SENARE appstart rensar
    # samma Event.
    _worker_stop_event = threading.Event()
    _worker_thread = threading.Thread(
        target=queue_worker_loop,
        args=(config.DATABASE_FILE, _worker_stop_event),
        daemon=True,
        name="queue-worker",
    )
    _worker_thread.start()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    """
    Signalerar arbetartråden att stanna och VÄNTAR IN att den faktiskt
    avslutats innan appen räknas som nedstängd. Utan detta join() skulle
    tråden kunna hinna göra ytterligare ett databasanrop efter att
    shutdown "returnerat" - ofarligt i produktion (processen lever kvar),
    men kan orsaka svårspårade, tidsberoende testfel när flera
    app-instanser startas/stängs i samma process (som i pytest-sviten,
    där nästa test hinner peka om databasen innan dess).
    """
    if _worker_stop_event is not None:
        _worker_stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Frontend (statiska filer)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
