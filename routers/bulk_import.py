"""
Router: bulk_import
CSV-bulkimport: lägger flera predikningar från en CSV-fil till i samma
bearbetningskö som manuellt klippta predikningar (routers/process.py), i
den ordning de listas i CSV-filen. Filerna klipps inte manuellt - hela
filen bearbetas, precis som om start/slut vore satt till hela ljudlängden.

Källfilen i BULK_IMPORT_DIR flyttas ALDRIG direkt - den kopieras in i
uploads/ under bearbetningen (se services/pipeline.py, keep_original=True)
och tas bort från BULK_IMPORT_DIR bara om raden bearbetas helt klart
(inklusive lyckad publicering) - se services/pipeline.py:_finish_bulk_item.
Misslyckas en rad ligger källfilen kvar orörd, vilket gör att samma
CSV-fil kan köras om: redan lyckade rader misslyckas då bara med "filen
hittades inte" (harmlöst, filen är redan importerad), medan resterande
rader bearbetas som vanligt.
"""
import csv
import io
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

import config
from modules import app_logging, queue_store

router = APIRouter(prefix="/api", tags=["bulk-import"])

BULK_COLUMN_ALIASES = {
    "filename": {"filename", "filnamn", "fil"},
    "speaker": {"speaker", "talare"},
    "date": {"date", "datum"},
    "time": {"time", "klockslag", "tid"},
    "title": {"title", "titel"},
}
BULK_REQUIRED_COLUMNS = {"filename", "speaker", "date", "time"}


def _normalize_csv_headers(fieldnames: Sequence[str] | None) -> dict[str, str]:
    """Mappar faktiska CSV-kolumnnamn (case-insensitive, svenska/engelska) till kanoniska nycklar."""
    mapping: dict[str, str] = {}
    for raw_name in fieldnames or []:
        key = raw_name.strip().lower()
        for canonical, aliases in BULK_COLUMN_ALIASES.items():
            if key in aliases:
                mapping[raw_name] = canonical
                break
    return mapping


def _parse_bulk_csv(raw_text: str) -> list[dict]:
    """
    Läser och validerar CSV-innehållets STRUKTUR (kolumner, datum/klockslag-
    format, obligatoriska fält). Kastar HTTPException(400) med en samlad,
    läsbar felbeskrivning om något är fel - hela batchen valideras innan
    något börjar bearbetas, så ett skrivfel i rad 8 inte upptäcks först
    efter att rad 1-7 redan bearbetats.

    OBS: om ljudfilen faktiskt finns i BULK_IMPORT_DIR kontrolleras
    medvetet INTE här, utan först när raden bearbetas (se
    services/pipeline.py:_run_queue_item). Det gör att samma CSV-fil kan
    köras om flera gånger - rader vars filer redan bearbetats klart (och
    därför tagits bort, se _finish_bulk_item) misslyckas då bara för just
    den raden, istället för att blockera hela importen.
    """
    reader = csv.DictReader(io.StringIO(raw_text))
    header_map = _normalize_csv_headers(reader.fieldnames)
    missing_columns = BULK_REQUIRED_COLUMNS - set(header_map.values())
    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CSV-filen saknar kolumn(er): {', '.join(sorted(missing_columns))}. "
                "Förväntade kolumner: filnamn (filename/filnamn), talare "
                "(speaker/talare), datum (date/datum), klockslag "
                "(time/klockslag/tid), samt valfritt titel (title/titel)."
            ),
        )

    items: list[dict] = []
    errors: list[str] = []

    for row_num, raw_row in enumerate(reader, start=2):  # rad 1 = header
        row = {
            canonical: (raw_row.get(raw_name) or "").strip()
            for raw_name, canonical in header_map.items()
        }
        filename = row.get("filename", "")
        speaker = row.get("speaker", "")
        date_str = row.get("date", "")
        time_str = row.get("time", "")
        title = row.get("title", "")

        row_errors: list[str] = []

        if not filename:
            row_errors.append("filnamn saknas")
        elif "/" in filename or "\\" in filename:
            # Skydd mot path traversal: pathlib ignorerar tyst BULK_IMPORT_DIR
            # och pekar rakt på sökvägen om filnamnet ser ut som en absolut
            # sökväg (t.ex. "C:/Windows/..."), och "../"-sekvenser kan på
            # samma sätt hamna utanför mappen. Ett riktigt filnamn innehåller
            # aldrig ett snedstreck.
            row_errors.append("filnamnet får inte innehålla en sökväg (\"/\" eller \"\\\") - ange bara själva filnamnet")
        elif Path(filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
            row_errors.append(f"filtyp stöds ej ('{Path(filename).suffix}')")

        if not speaker:
            row_errors.append("talare saknas")

        publish_date = ""
        if not date_str or not time_str:
            row_errors.append("datum eller klockslag saknas")
        else:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                publish_date = dt.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                row_errors.append(
                    f"ogiltigt datum/klockslag ('{date_str} {time_str}', "
                    "förväntat format ÅÅÅÅ-MM-DD och TT:MM)"
                )

        if row_errors:
            errors.append(f"Rad {row_num} ({filename or '?'}): {'; '.join(row_errors)}")
            continue

        items.append({
            "filename": filename,
            "speaker": speaker,
            "title": title,
            "publish_date": publish_date,
        })

    if errors:
        raise HTTPException(status_code=400, detail="Fel i CSV-filen:\n" + "\n".join(errors))
    if not items:
        raise HTTPException(status_code=400, detail="CSV-filen innehöll inga datarader att importera.")

    return items


@router.post("/bulk-import")
async def bulk_import(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV-filen måste vara UTF-8-kodad.") from exc

    try:
        items = _parse_bulk_csv(raw_text)
    except HTTPException as exc:
        app_logging.logger.error(f"Bulkimport: CSV-filen '{file.filename}' avvisades - {exc.detail}")
        raise

    queue_items = []
    for item in items:
        job_id = str(uuid.uuid4())
        queue_id = str(uuid.uuid4())
        fields = {
            "start_seconds": 0,
            "end_seconds": 0,  # sätts av services/pipeline.py:_run_queue_item precis innan bearbetning
            "speaker": item["speaker"],
            "title": item["title"],
            "description": "",
            "category": "",
            "publish_date": item["publish_date"],
        }
        original_path = config.BULK_IMPORT_DIR / item["filename"]
        queue_store.add(
            queue_id, job_id, "bulk", item["filename"], item["speaker"], fields,
            original_path, True, datetime.now().isoformat(),
        )
        queue_items.append({"job_id": job_id, "queue_id": queue_id, "filename": item["filename"], "speaker": item["speaker"]})

    app_logging.logger.info(
        f"Bulkimport: {len(queue_items)} rad(er) från '{file.filename}' tillagda i bearbetningskön"
    )

    return {"items": queue_items}
