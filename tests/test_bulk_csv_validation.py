"""
Tester för routers/bulk_import.py:_parse_bulk_csv - CSV-strukturvalidering
vid bulkimport, inklusive skyddet mot path traversal (se kodgranskningen
som identifierade att BULK_IMPORT_DIR/filnamn annars tyst kunde peka
utanför BULK_IMPORT_DIR om filnamnet innehöll "/" eller "\\").
"""
import pytest
from fastapi import HTTPException

from routers.bulk_import import _parse_bulk_csv


def test_missing_required_columns_rejected():
    """
    En CSV som saknar obligatoriska kolumner (datum och klockslag) avvisas
    med ett fel som talar om vilka som saknas.
    """
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv("filnamn,talare\na.mp3,Anna\n")
    assert exc_info.value.status_code == 400
    assert "saknar kolumn" in exc_info.value.detail


def test_valid_csv_accepted():
    """
    En giltig rad blir en post med datum och klockslag i formulärets format.
    """
    items = _parse_bulk_csv("filnamn,talare,datum,klockslag\na.mp3,Anna,2026-01-01,10:00\n")
    assert items == [{"filename": "a.mp3", "speaker": "Anna", "title": "", "publish_date": "2026-01-01T10:00"}]


def test_swedish_and_english_column_aliases_both_work():
    """
    Svenska och engelska kolumnnamn ger exakt samma resultat.
    """
    items_sv = _parse_bulk_csv("filnamn,talare,datum,klockslag,titel\na.mp3,Anna,2026-01-01,10:00,Min titel\n")
    items_en = _parse_bulk_csv("filename,speaker,date,time,title\na.mp3,Anna,2026-01-01,10:00,Min titel\n")
    assert items_sv == items_en


@pytest.mark.parametrize("filename", [
    "../secret.mp3",
    "../../etc/passwd.mp3",
    "C:/Windows/System32/evil.mp3",
    "C:\\Windows\\System32\\evil.mp3",
    "subdir/file.mp3",
])
def test_path_traversal_filenames_rejected(filename):
    """
    Filnamn som innehåller en sökväg avvisas - annars kunde en CSV peka
    ut en fil utanför bulk_import/ (t.ex. "../" eller "C:/Windows/...").
    """
    csv_text = f"filnamn,talare,datum,klockslag\n{filename},Anna,2026-01-01,10:00\n"
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv(csv_text)
    assert exc_info.value.status_code == 400
    assert "sökväg" in exc_info.value.detail


def test_unsupported_file_extension_rejected():
    """
    Filtyper som inte är ljud avvisas redan när CSV-filen kontrolleras.
    """
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv("filnamn,talare,datum,klockslag\na.txt,Anna,2026-01-01,10:00\n")
    assert "filtyp" in exc_info.value.detail


def test_invalid_date_or_time_rejected():
    """
    Datum i fel format (här amerikanskt) avvisas med en förklaring.
    """
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv("filnamn,talare,datum,klockslag\na.mp3,Anna,01/01/2026,10:00\n")
    assert "ogiltigt datum" in exc_info.value.detail


def test_missing_speaker_rejected():
    """
    Talaren är obligatorisk även vid bulkimport.
    """
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv("filnamn,talare,datum,klockslag\na.mp3,,2026-01-01,10:00\n")
    assert "talare saknas" in exc_info.value.detail


def test_empty_csv_rejected():
    """
    En fil med bara rubrikrad avvisas - det finns inget att importera.
    """
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv("filnamn,talare,datum,klockslag\n")
    assert "inga datarader" in exc_info.value.detail


def test_multiple_row_errors_all_reported_together():
    """
    Alla felaktiga rader rapporteras på en gång (här rad 2 och 3), så att
    användaren kan rätta hela filen i ett svep.
    """
    csv_text = (
        "filnamn,talare,datum,klockslag\n"
        "a.mp3,,2026-01-01,10:00\n"
        "b.mp3,Bertil,ogiltigt,11:00\n"
    )
    with pytest.raises(HTTPException) as exc_info:
        _parse_bulk_csv(csv_text)
    detail = exc_info.value.detail
    assert "Rad 2" in detail
    assert "Rad 3" in detail
