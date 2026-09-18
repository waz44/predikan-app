"""
Modul: transcription_worker
Håller en långlivad bakgrundsprocess (transcription_worker_process.py -
se den filens docstring för varför den är en egen process istället för
att använda Pythons multiprocessing-modul) som utför den faktiska
transkriberingen, istället för att köra den i huvudprocessens
bearbetningstråd (samma tråd som hela bearbetningskön, se app.py).

Det gör att en pågående transkribering - den överlägset mest tidskrävande
delen av pipelinen, särskilt med lokal Whisper på CPU - går att avbryta på
riktigt genom att döda processen (CPU/GPU frigörs omedelbart), istället
för att bara markeras som avbruten medan beräkningen fortsätter osynligt
i bakgrunden.

Processen återanvänds mellan jobb, så en lokal Whisper-modell bara behöver
laddas en gång. Avbryts en transkribering dödas processen och en ny
startas automatiskt åt nästa jobb - då laddas modellen om (några sekunders
extra fördröjning just då), annars ingen skillnad mot att köra i samma
process.
"""
import json
import queue as queue_module
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

_WORKER_SCRIPT = Path(__file__).resolve().parent / "transcription_worker_process.py"

_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None


class TranscriptionCancelled(Exception):
    """Kastas av transcribe() när cancel_event sätts under en pågående transkribering."""


def _spawn_worker(base_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_WORKER_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # radbuffrat, så varje JSON-rad skickas/tas emot direkt
        cwd=str(base_dir),
    )


def _ensure_worker(base_dir: Path) -> subprocess.Popen:
    global _process
    with _lock:
        if _process is None or _process.poll() is not None:
            _process = _spawn_worker(base_dir)
        return _process


def _kill_worker(process: subprocess.Popen) -> None:
    global _process
    try:
        process.kill()
    except Exception:
        pass
    with _lock:
        if _process is process:
            _process = None


def transcribe(path: Path, base_dir: Path, cancel_event: threading.Event) -> str:
    """
    Skickar en transkriberingsförfrågan till bakgrundsprocessen och väntar
    på svaret - men avbryter (dödar processen) om cancel_event sätts under
    tiden, och kastar då TranscriptionCancelled. En ny process startas
    automatiskt vid nästa anrop.
    """
    process = _ensure_worker(base_dir)

    try:
        process.stdin.write(json.dumps({"path": str(path)}) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        _kill_worker(process)
        raise RuntimeError(f"Kunde inte skicka till transkriberingsprocessen: {exc}") from exc

    result_queue: "queue_module.Queue[tuple[str, str]]" = queue_module.Queue(maxsize=1)

    def _read_response() -> None:
        try:
            line = process.stdout.readline()
        except Exception as exc:
            result_queue.put(("error", str(exc)))
            return
        result_queue.put(("line", line))

    threading.Thread(target=_read_response, daemon=True).start()

    while True:
        if cancel_event.is_set():
            _kill_worker(process)
            raise TranscriptionCancelled("Transkriberingen avbröts av användaren.")
        try:
            kind, payload = result_queue.get(timeout=0.3)
            break
        except queue_module.Empty:
            continue

    if kind == "error" or not payload:
        stderr_tail = ""
        try:
            stderr_tail = (process.stderr.read(2000) or "").strip()
        except Exception:
            pass
        _kill_worker(process)
        detail = f" {stderr_tail}" if stderr_tail else ""
        raise RuntimeError(f"Transkriberingsprocessen avslutades oväntat.{detail}")

    response = json.loads(payload)
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "Okänt fel i transkriberingsprocessen.")
    return response["transcript"]
