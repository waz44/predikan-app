"""
Modul: storage_cleanup
Ad-hoc-städning av filer från ett enskilt misslyckat eller avbrutet
bearbetningsförsök - t.ex. en rad i CSV-bulkimport som misslyckades (se
app.py:_finish_bulk_item), eller ett manuellt jobb som avbröts (se
app.py:_run_queue_item). I båda fallen finns ingen färdig episod att slå
upp (den blev aldrig klar), så här städas filerna direkt via ett enkelt
filnamnsmönster istället.

Den långsiktiga lagringsbegränsningen (MAX_STORED_EPISODES - hur många
FÄRDIGA episoder som sparas totalt) sköts numera av
modules/episode_store.py:enforce_retention, som har databasen (tabellen
episodes) som källa till sanning för vilka episoder som finns och i
vilken ordning de skapades, istället för att som tidigare tolka det ur
filnamn i uploads/+processed/.
"""
from glob import escape as glob_escape
from pathlib import Path


def delete_episode_files(upload_dir: Path, processed_dir: Path, base_name: str) -> None:
    """
    Tar bort alla filer i upload_dir/processed_dir som hör till ett specifikt
    bas-filnamn. Används för att städa undan resultatet av ett misslyckat
    eller avbrutet bearbetningsförsök i CSV-bulkimport (se
    app.py:_finish_bulk_item), där originalfilen ändå finns bevarad orörd i
    BULK_IMPORT_DIR - så en ny körning inte lämnar kvar halvfärdiga filer
    från tidigare försök.
    """
    for p in upload_dir.glob(f"{glob_escape(base_name)}.*"):
        try:
            p.unlink()
        except OSError:
            pass
    delete_processed_files(processed_dir, base_name)


def delete_processed_files(processed_dir: Path, base_name: str) -> None:
    """
    Tar bort bara processed/-filerna för ett bas-filnamn (INTE originalet i
    uploads/). Används när ett manuellt köobjekt avbryts (se
    app.py:_run_queue_item) - originalfilen i uploads/ ska då finnas kvar
    (det kan vara användarens enda kopia av ljudet), men de ofärdiga
    resultatfilerna (klippt ljud, transkript, AI-debugfiler m.m.) städas
    bort.
    """
    for p in processed_dir.glob(f"{glob_escape(base_name)}-*"):
        try:
            p.unlink()
        except OSError:
            pass
