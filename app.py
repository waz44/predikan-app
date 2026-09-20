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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from modules import app_logging, db, queue_store
from routers import bulk_import, process, queue, spreaker_episodes, stats, upload
from services.pipeline import queue_worker_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Appens livscykel: startar den enda bearbetningskö-arbetartråden vid
    uppstart och väntar in att den avslutas vid nedstängning. Ersätter de
    tidigare @app.on_event("startup"/"shutdown")-hookarna (utfasade i
    FastAPI). TestClient kör detta som en context manager, precis som en
    riktig uvicorn-körning, så start/stopp-beteendet är identiskt.
    """
    # ---- Uppstart ----
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
    # dessutom att bara denna livscykelns egen nedstängning någonsin kan
    # stoppa/återuppliva den - en delad global skulle kunna återuppliva en
    # föregående tests kvarlevande tråd (vars join() nedan hann ge upp
    # innan tråden faktiskt avslutat sig) när en SENARE appstart rensar
    # samma Event.
    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=queue_worker_loop,
        args=(config.DATABASE_FILE, stop_event),
        daemon=True,
        name="queue-worker",
    )
    worker_thread.start()

    yield

    # ---- Nedstängning ----
    # Signalerar arbetartråden att stanna och VÄNTAR IN att den faktiskt
    # avslutats innan appen räknas som nedstängd. Utan detta join() skulle
    # tråden kunna hinna göra ytterligare ett databasanrop efter att
    # nedstängningen "returnerat" - ofarligt i produktion (processen lever
    # kvar), men kan orsaka svårspårade, tidsberoende testfel när flera
    # app-instanser startas/stängs i samma process (som i pytest-sviten,
    # där nästa test hinner peka om databasen innan dess).
    stop_event.set()
    worker_thread.join(timeout=5)


app = FastAPI(title="Predikan → Podcast", lifespan=lifespan)

app.include_router(upload.router)
app.include_router(process.router)
app.include_router(queue.router)
app.include_router(bulk_import.router)
app.include_router(stats.router)
app.include_router(spreaker_episodes.router)

# ---------------------------------------------------------------------------
# Frontend (statiska filer)
# ---------------------------------------------------------------------------
# Absolut sökväg (via config.BASE_DIR) i stället för relativa "static", så
# appen fungerar oavsett vilken katalog den startas ifrån - t.ex. via
# konsollkommandot `predikan` (se main() nedan), som kan köras var som helst.
app.mount("/", StaticFiles(directory=str(config.BASE_DIR / "static"), html=True), name="static")


def main() -> None:
    """
    Startpunkt för konsollkommandot `predikan` (se pyproject.toml).
    Startar en uvicorn-server. Host/port/reload styrs av miljövariablerna
    HOST (standard 127.0.0.1), PORT (standard 8000) och RELOAD (1/true för
    autoreload vid kodändring under utveckling).

    Motsvarar att köra `uvicorn app:app` för hand, men slipper minnas
    kommandot och fungerar från valfri katalog efter `pip install`.
    """
    import os

    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
