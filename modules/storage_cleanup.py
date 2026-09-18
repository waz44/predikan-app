"""
Modul: storage_cleanup
Håller uploads/- och processed/-mapparna begränsade till ett fast antal
predikningar (episoder), så att disken inte växer oändligt vid drift över
lång tid. Styrs av MAX_STORED_EPISODES i .env (0/tomt = ingen begränsning,
städa aldrig bort något).

En "episod" = originalfilen i uploads/ (döpt "<bas>.mp3/.wav") + alla filer
i processed/ vars namn börjar med "<bas>-" (klippt ljud, transkript,
AI-berikning, AI-debugfiler, m.m. - se app.py:_run_processing_job och
modules/ai_enrichment.py:_save_debug). Grupperingen är medvetet generisk
istället för en hårdkodad lista filändelser, så att den automatiskt täcker
alla filtyper pipelinen skriver till processed/, nu och i framtiden.

Filer som ännu inte bearbetats (uppladdade men bearbetningen inte startad,
fortfarande med sitt ursprungliga uuid-filnamn) saknar matchande filer i
processed/ och rörs därför aldrig av städningen.
"""
from glob import escape as glob_escape
from pathlib import Path


def _episode_groups(upload_dir: Path, processed_dir: Path) -> dict[str, list[Path]]:
    """Grupperar filer i upload_dir/processed_dir efter delat bas-filnamn."""
    groups: dict[str, list[Path]] = {}
    for p in upload_dir.glob("*"):
        if not p.is_file():
            continue
        base = p.stem
        matches = [q for q in processed_dir.glob(f"{glob_escape(base)}-*") if q.is_file()]
        if matches:
            groups[base] = [p, *matches]

    return groups


def enforce_retention(upload_dir: Path, processed_dir: Path, max_episodes: int) -> list[str]:
    """
    Tar bort de äldsta färdigbearbetade episoderna tills högst
    `max_episodes` återstår (sorterat på senast ändrad fil per episod).

    Returnerar bas-filnamnen på de episoder som togs bort, så anroparen kan
    städa egna referenser till dem (t.ex. UPLOADED_FILES i app.py).
    """
    if max_episodes <= 0:
        return []

    groups = _episode_groups(upload_dir, processed_dir)
    if len(groups) <= max_episodes:
        return []

    episodes = [
        (max(f.stat().st_mtime for f in files), base, files)
        for base, files in groups.items()
    ]
    episodes.sort(key=lambda item: item[0], reverse=True)  # nyast först

    removed_bases: list[str] = []
    for _, base, files in episodes[max_episodes:]:
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        removed_bases.append(base)

    return removed_bases


def delete_episode_files(upload_dir: Path, processed_dir: Path, base_name: str) -> None:
    """
    Tar bort alla filer i upload_dir/processed_dir som hör till ett specifikt
    bas-filnamn. Används för att städa undan resultatet av ett misslyckat
    bearbetningsförsök (t.ex. i CSV-bulkimport, se app.py:_run_bulk_batch),
    så en ny körning inte lämnar kvar halvfärdiga filer från tidigare försök.
    """
    for p in upload_dir.glob(f"{glob_escape(base_name)}.*"):
        try:
            p.unlink()
        except OSError:
            pass
    for p in processed_dir.glob(f"{glob_escape(base_name)}-*"):
        try:
            p.unlink()
        except OSError:
            pass
