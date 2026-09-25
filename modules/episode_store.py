"""
Modul: episode_store
Databaslager för episodhistoriken (SQLite-tabellen episodes, se
modules/db.py). En rad skrivs per LYCKAD bearbetning (se
app.py:_run_queue_item). Ersätter två tidigare fristående, filbaserade
lösningar:

- modules/stats.py (stats.json) - statistiken beräknas nu istället som en
  aggregatfråga över episodes, se get_stats()/estimate_processing_seconds().
- Den filnamnsbaserade grupperingen i modules/storage_cleanup.py
  (enforce_retention) - vilka episoder som finns och i vilken ordning de
  skapades avgörs nu av databasen istället för att tolkas ur filnamn i
  uploads/+processed/, vilket tidigare av misstag missade en filtyp
  (AI-debugfiler) som inte följde den förväntade namnkonventionen.

Den faktiska filborttagningen vid rensning återanvänder ändå ett enkelt,
generiskt glob-mönster mot processed/ (fångar klippt ljud, transkript,
AI-berikning och eventuella framtida filtyper oavsett vad de heter) -
bara VILKA episoder som är "för gamla" kommer numera från databasen.
"""
# json: taggarna lagras som en JSON-lista i en textkolumn.
import json

# glob_escape: gör tecken som [ ] * ? i ett filnamn ofarliga i ett
# glob-mönster, så att "Predikan [del 1]" inte tolkas som jokertecken.
from glob import escape as glob_escape

# Path: sökvägar till filerna som ska tas bort vid lagringsstädning.
from pathlib import Path

# db.get_connection() ger en kortlivad SQLite-anslutning per anrop.
from modules import db


def record_episode(data: dict) -> None:
    """
    Sparar en lyckat avslutad episod. `data` speglar formen på job["result"] plus lite extra (se app.py).

    Om samma base_name redan finns (samma jobb sparat två gånger) uppdateras
    bara länk och bearbetningstid - övriga fält skrivs inte över.

    Args:
        data: Fälten för episoden. Obligatoriska: base_name, speaker, kind,
            sermon_seconds, processing_seconds och created_at. Övriga får
            saknas och sparas då som NULL (eller 0 för flaggorna).
    """
    with db.get_connection() as conn:
        # ON CONFLICT ... DO UPDATE ("upsert"): finns base_name redan
        # uppdateras bara länk och tid i stället för att frågan misslyckas.
        # SQLite saknar booleska värden, därav int(bool(...)) för flaggorna.
        conn.execute(
            """
            INSERT INTO episodes (
                base_name, speaker, title, description, tags, publish_date, category, kind,
                episode_url, simulated, scheduled, backdated, email_sent,
                audio_path, transcript_path, enrichment_path,
                sermon_seconds, processing_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(base_name) DO UPDATE SET
                episode_url = excluded.episode_url,
                processing_seconds = excluded.processing_seconds
            """,
            (
                data["base_name"],
                data["speaker"],
                data.get("title"),
                data.get("description"),
                json.dumps(data.get("tags") or []),
                data.get("publish_date"),
                data.get("category"),
                data["kind"],
                data.get("episode_url"),
                int(bool(data.get("simulated"))),
                int(bool(data.get("scheduled"))),
                int(bool(data.get("backdated"))),
                int(bool(data.get("email_sent"))),
                data.get("audio_path"),
                data.get("transcript_path"),
                data.get("enrichment_path"),
                data["sermon_seconds"],
                data["processing_seconds"],
                data["created_at"],
            ),
        )


def get_stats() -> dict:
    """
    Ackumulerad statistik + processing_ratio (None om ingen historik finns än).

    Visas i rutan "Prestandastatistik" och ligger till grund för
    tidsuppskattningen (se estimate_processing_seconds).

    Returns:
        En dict med total_count (antal predikningar), total_sermon_seconds
        (summa predikanlängd), total_processing_seconds (summa
        bearbetningstid) och processing_ratio (bearbetningssekunder per
        sekund predikan, eller None innan första predikan).
    """
    with db.get_connection() as conn:
        # En enda aggregatfråga räknar allt på en gång. COALESCE(..., 0)
        # behövs eftersom SUM över en tom tabell ger NULL, inte 0.
        row = conn.execute(
            "SELECT COUNT(*) AS total_count, "
            "COALESCE(SUM(sermon_seconds), 0) AS total_sermon_seconds, "
            "COALESCE(SUM(processing_seconds), 0) AS total_processing_seconds "
            "FROM episodes"
        ).fetchone()

    total_sermon_seconds = row["total_sermon_seconds"]
    total_processing_seconds = row["total_processing_seconds"]
    # Kvoten räknas på summorna (inte som ett snitt av varje predikans
    # kvot), så långa predikningar väger tyngre - det ger en bättre
    # uppskattning för typiska predikningar på 30-60 minuter.
    ratio = total_processing_seconds / total_sermon_seconds if total_sermon_seconds > 0 else None

    return {
        "total_count": row["total_count"],
        "total_sermon_seconds": total_sermon_seconds,
        "total_processing_seconds": total_processing_seconds,
        "processing_ratio": ratio,
    }


def estimate_processing_seconds(sermon_seconds: float) -> float | None:
    """
    Uppskattar bearbetningstid (sekunder) för en predikan av given längd,
    baserat på historiskt snitt (se get_stats). Returnerar None om ingen
    historik finns än - anroparen avgör själv hur avsaknaden av en
    uppskattning ska visas (se services/pipeline.py och static/app.js, som
    visar det tydligt istället för att gissa utifrån en påhittad kvot).

    Exempel: har predikningar hittills tagit i snitt 0,5 s per sekund ljud
    uppskattas en predikan på 40 min (2400 s) ta 1200 s = 20 min.

    Args:
        sermon_seconds: Predikans (klippets) längd i sekunder.

    Returns:
        Uppskattad tid i sekunder, eller None utan historik.
    """
    ratio = get_stats()["processing_ratio"]
    if ratio is None:
        # Ingen historik än - hellre ingen uppskattning än en påhittad.
        return None
    # max(0.0, ...) skyddar mot en negativ längd om slut < start i formuläret.
    return max(0.0, sermon_seconds) * ratio


def enforce_retention(processed_dir: Path, max_episodes: int) -> list[str]:
    """
    Tar bort de äldsta episoderna (och deras filer) tills högst
    `max_episodes` återstår. Returnerar bas-filnamnen på de episoder som
    togs bort, så anroparen kan städa egna referenser (t.ex.
    UPLOADED_FILES i app.py).

    Args:
        processed_dir: Mappen med genererade filer (processed/).
        max_episodes: Hur många episoder som får finnas kvar. 0 eller mindre
            betyder obegränsat - då tas ingenting bort.

    Returns:
        Basnamnen på borttagna episoder (tom lista om inget togs bort).
    """
    # 0 (standard) = ingen gräns, städa aldrig.
    if max_episodes <= 0:
        return []

    # Alla episoder, nyaste först - de första max_episodes ska behållas.
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT base_name, audio_path FROM episodes ORDER BY created_at DESC"
        ).fetchall()

    # Under gränsen: inget att göra.
    if len(rows) <= max_episodes:
        return []

    # Allt efter de max_episodes nyaste är för gammalt.
    to_remove = rows[max_episodes:]
    removed_bases: list[str] = []

    with db.get_connection() as conn:
        for row in to_remove:
            base_name = row["base_name"]
            audio_path: str | None = row["audio_path"]

            # Det klippta ljudet ligger på en känd sökväg - ta bort den först.
            if audio_path:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except OSError:
                    # En låst fil får inte stoppa städningen av resten.
                    pass

            # Därefter ALLA övriga filer i processed/ som börjar med basnamnet
            # (transkript, AI-berikning, AI:ns felsökningsfiler m.m.) - så även
            # filtyper som läggs till i framtiden städas automatiskt.
            for p in processed_dir.glob(f"{glob_escape(base_name)}-*"):
                try:
                    p.unlink()
                except OSError:
                    pass

            # Sist raden i databasen, så episoden inte längre räknas.
            conn.execute("DELETE FROM episodes WHERE base_name = ?", (base_name,))
            removed_bases.append(base_name)

    return removed_bases
