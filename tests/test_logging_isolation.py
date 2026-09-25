"""Testerna ska aldrig skriva i den riktiga app.log i projektmappen (se conftest.py)."""
import logging
from pathlib import Path

import config


def test_tests_log_to_temporary_file():
    """
    Loggaren skriver till en fil utanför projektmappen under testerna -
    aldrig till den riktiga app.log.
    """
    from modules import app_logging

    app_logging.logger.info("Loggrad från testsviten")
    paths = [Path(h.baseFilename) for h in logging.getLogger("predikan").handlers if hasattr(h, "baseFilename")]
    assert paths, "loggaren ska ha en filhanterare"
    for path in paths:
        assert path != config.BASE_DIR / "app.log"
        assert config.BASE_DIR not in path.parents
