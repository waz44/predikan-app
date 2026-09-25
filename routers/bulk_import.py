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
# csv: Pythons inbyggda CSV-läsare, som klarar citattecken och kommatecken i fält.
import csv

# io.StringIO: låter CSV-läsaren läsa en textsträng som om den vore en fil.
import io

# uuid: unika id:n för varje köad rad.
import uuid

# Sequence: typen för listan med kolumnrubriker.
from collections.abc import Sequence

# datetime: tolka och formatera datum och klockslag.
from datetime import datetime

# Path: kontrollera filändelsen på filnamnen.
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

import config
from modules import app_logging, queue_store

# Alla endpoints här börjar med /api (bara /api/bulk-import finns).
router = APIRouter(prefix="/api", tags=["bulk-import"])

# Tillåtna kolumnrubriker per fält, på svenska och engelska. Rubrikerna
# jämförs i gemener, så "Filnamn", "FILNAMN" och "filnamn" fungerar alla.
BULK_COLUMN_ALIASES = {
    "filename": {"filename", "filnamn", "fil"},
    "speaker": {"speaker", "talare"},
    "date": {"date", "datum"},
    "time": {"time", "klockslag", "tid"},
    "title": {"title", "titel"},
}
# Titeln är valfri (tom = AI:n skriver en) - övriga kolumner måste finnas.
BULK_REQUIRED_COLUMNS = {"filename", "speaker", "date", "time"}


def _normalize_csv_headers(fieldnames: Sequence[str] | None) -> dict[str, str]:
    """
    Mappar faktiska CSV-kolumnnamn (case-insensitive, svenska/engelska) till kanoniska nycklar.

    Args:
        fieldnames: Kolumnrubrikerna som de står i CSV-filen.

    Returns:
        En karta från CSV-filens rubrik till appens interna fältnamn
        ("filnamn"/"filename" -> "filename" osv.).
    """
    mapping: dict[str, str] = {}
    # fieldnames är None om filen är helt tom - då blir kartan tom.
    for raw_name in fieldnames or []:
        key = raw_name.strip().lower()
        # Hitta vilket fält rubriken hör till. Okända rubriker (t.ex. en
        # egen anteckningskolumn) ignoreras helt.
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

    Args:
        raw_text: CSV-filens innehåll som text.

    Returns:
        En lista med en dict per rad (filnamn, talare, datum, klockslag, titel).

    Raises:
        HTTPException 400: Med en samlad beskrivning av alla fel i filen.
    """
    # DictReader läser första raden som rubriker och ger sedan varje rad som
    # en dict med rubrikerna som nycklar.
    reader = csv.DictReader(io.StringIO(raw_text))
    header_map = _normalize_csv_headers(reader.fieldnames)
    # Mängdskillnad: de obligatoriska fält som ingen rubrik motsvarar.
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

    # Giltiga rader samlas i items och ALLA fel i errors - så att användaren
    # får se samtliga problem på en gång i stället för ett i taget.
    items: list[dict] = []
    errors: list[str] = []

    for row_num, raw_row in enumerate(reader, start=2):  # rad 1 = header
        # Byt CSV-filens rubriker mot de interna fältnamnen och ta bort
        # blanksteg runt värdena. Saknade värden blir "".
        row = {
            canonical: (raw_row.get(raw_name) or "").strip()
            for raw_name, canonical in header_map.items()
        }
        filename = row.get("filename", "")
        speaker = row.get("speaker", "")
        date_str = row.get("date", "")
        time_str = row.get("time", "")
        title = row.get("title", "")

        # Alla fel på just den här raden, så de kan visas tillsammans.
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
                # Datum och klockslag tolkas tillsammans; strptime kontrollerar
                # samtidigt att datumet finns (t.ex. inte 2024-02-30).
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                # Samma format som formulärets datumfält ("2024-03-10T11:00"),
                # så resten av flödet inte behöver skilja på varifrån det kom.
                publish_date = dt.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                row_errors.append(
                    f"ogiltigt datum/klockslag ('{date_str} {time_str}', "
                    "förväntat format ÅÅÅÅ-MM-DD och TT:MM)"
                )

        # En felaktig rad köas inte - men kontrollen fortsätter med nästa rad.
        if row_errors:
            errors.append(f"Rad {row_num} ({filename or '?'}): {'; '.join(row_errors)}")
            continue

        items.append({
            "filename": filename,
            "speaker": speaker,
            "title": title,
            "publish_date": publish_date,
        })

    # Minst ett fel någonstans: hela importen stoppas, INGENTING köas.
    if errors:
        raise HTTPException(status_code=400, detail="Fel i CSV-filen:\n" + "\n".join(errors))
    # Bara en rubrikrad, inga data.
    if not items:
        raise HTTPException(status_code=400, detail="CSV-filen innehöll inga datarader att importera.")

    return items


@router.post("/bulk-import")
async def bulk_import(file: UploadFile = File(...)):
    """
    POST /api/bulk-import - läser en CSV-fil och lägger varje rad i bearbetningskön.

    Ljudfilerna ska redan ligga i BULK_IMPORT_DIR. Hela filen kontrolleras
    före import: är någon rad felaktig köas INGENTING, så att en halv import
    aldrig blir kvar. Filernas existens kontrolleras först när varje rad
    bearbetas (se pipeline._run_queue_item).

    Args:
        file: CSV-filen från formuläret.

    Returns:
        {"items": [...]} med job_id, queue_id, filnamn och talare för varje
        köad rad.

    Raises:
        HTTPException 400: Om filen inte är en giltig CSV med rätt kolumner.
    """
    # CSV-filer är små - hela filen läses in på en gång.
    raw_bytes = await file.read()
    try:
        # utf-8-sig tar bort den osynliga BOM som Excel lägger först i
        # UTF-8-filer - annars skulle första rubriken bli "﻿filnamn"
        # och inte kännas igen.
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # T.ex. en fil sparad som "CSV (semikolonavgränsad)" i äldre Excel,
        # som använder Windows egen teckenkodning.
        raise HTTPException(status_code=400, detail="CSV-filen måste vara UTF-8-kodad.") from exc

    try:
        items = _parse_bulk_csv(raw_text)
    except HTTPException as exc:
        # Logga varför filen avvisades och skicka felet vidare till sidan.
        app_logging.logger.error(f"Bulkimport: CSV-filen '{file.filename}' avvisades - {exc.detail}")
        raise

    # Hela filen är godkänd - nu köas varje rad, i CSV-filens ordning.
    queue_items = []
    for item in items:
        job_id = str(uuid.uuid4())
        queue_id = str(uuid.uuid4())
        # Samma fält som formuläret skickar (se pipeline.ProcessRequest).
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
        # kind="bulk" och keep_original=True: filen kopieras in i uploads/
        # och tas bort från bulk_import/ först när raden lyckats.
        queue_store.add(
            queue_id, job_id, "bulk", item["filename"], item["speaker"], fields,
            original_path, True, datetime.now().isoformat(),
        )
        queue_items.append({"job_id": job_id, "queue_id": queue_id, "filename": item["filename"], "speaker": item["speaker"]})

    app_logging.logger.info(
        f"Bulkimport: {len(queue_items)} rad(er) från '{file.filename}' tillagda i bearbetningskön"
    )

    return {"items": queue_items}
