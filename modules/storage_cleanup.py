"""
Modul: storage_cleanup
Håller uploads/- och processed/-mapparna begränsade till ett fast antal
predikningar (episoder), så att disken inte växer oändligt vid drift över
lång tid. Styrs av MAX_STORED_EPISODES i .env (0/tomt = ingen begränsning,
städa aldrig bort något).

En "episod" = originalfilen i uploads/ (döpt "<bas>.mp3/.wav") + de tre
genererade filerna i processed/ ("<bas>-clipped.mp3", "<bas>-transcript.txt",
"<bas>-enrichning.json") som delar samma bas-filnamn (se app.py:_run_processing_job).

Filer som ännu inte bearbetats (uppladdade men bearbetningen inte startad,
fortfarande med sitt ursprungliga uuid-filnamn) saknar en matchande fil i
processed/ och rörs därför aldrig av städningen.
"""
from pathlib import Path

PROCESSED_SUFFIXES = ("-clipped.mp3", "-transcript.txt", "-enrichment.json")


def _episode_groups(upload_dir: Path, processed_dir: Path) -> dict[str, list[Path]]:
    """Grupperar filer i upload_dir/processed_dir efter delat bas-filnamn."""
    groups: dict[str, list[Path]] = {}
    for p in processed_dir.glob("*"):
        if not p.is_file():
            continue
        for suffix in PROCESSED_SUFFIXES:
            if p.name.endswith(suffix):
                groups.setdefault(p.name[: -len(suffix)], []).append(p)
                break

    for p in upload_dir.glob("*"):
        if p.is_file() and p.stem in groups:
            groups[p.stem].append(p)

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
