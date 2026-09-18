"""
Modul: app_logging
Konfigurerar en gemensam loggare för hela applikationen, som skriver till
en loggfil (config.LOG_FILE, standard "app.log" i projektroten) på en
loggnivå som styrs av LOG_LEVEL i .env (DEBUG/INFO/WARNING/ERROR/CRITICAL,
standard INFO).

Används i första hand för att följa vad som händer under en CSV-
bulkimport (vilken fil som bearbetas, lyckad publicering, eller varför en
rad misslyckades) - se app.py:_run_bulk_batch.
"""
import logging
import config

logger = logging.getLogger("predikan")

if not logger.handlers:  # undvik dubbla handlers vid omladdning (t.ex. uvicorn --reload)
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logger.setLevel(level)

    _handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
