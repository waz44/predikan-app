"""
Modul: transcription_worker_process
Startpunkt för den separata bakgrundsprocess som utför själva
transkriberingen (se modules/transcription_worker.py för hur
huvudprocessen pratar med den här processen och varför).

Körs som en fristående process - INTE via Pythons multiprocessing-modul,
som har finurliga begränsningar kring hur "huvudmodulen" återimporteras
i barnprocessen (särskilt på Windows, där appen körs via `uvicorn
app:app`). Kommunikationen sker istället över processens vanliga
stdin/stdout med en rad JSON per förfrågan/svar. Det gör processen
trivial att döda utifrån (för att avbryta en pågående transkribering)
utan att det stör huvudprocessen eller kräver att pickling fungerar.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skydda IPC-protokollet (en JSON-rad per svar på stdout) mot att något i
# transkriberingsvägen (Whisper/PyTorch/tqdm/varningar m.m.) råkar skriva
# något annat till stdout - t.ex. en diagnostikrad vid modell-laddning
# (se modules/transcription.py) - vilket annars tyst skulle göra svaret
# till huvudprocessen oläsbart. Den riktiga stdout sparas undan och
# används bara av _write_response() nedan; allt annat som råkar skrivas
# till "stdout" (inklusive av importerade bibliotek) hamnar på stderr
# istället, där det inte stör protokollet.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

from modules import transcription  # noqa: E402  (måste importeras efter sys.path/stdout-fixen ovan)


def _write_response(response: dict) -> None:
    # MEDVETET ren ASCII (json.dumps standard: å -> å). En omdirigerad
    # stdout använder på Windows systemets teckentabell (cp1252), inte UTF-8,
    # medan huvudprocessen läser som UTF-8 - med ensure_ascii=False blev
    # därför varje å/ä/ö i transkriptet ett "�" (tyst, via errors="replace"
    # i modules/transcription_worker.py). ASCII-JSON fungerar oavsett
    # teckentabell i båda ändar.
    _real_stdout.write(json.dumps(response) + "\n")
    _real_stdout.flush()


def main() -> None:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            request = json.loads(raw_line)
            transcript = transcription.transcribe_audio(Path(request["path"]))
            response = {"ok": True, "transcript": transcript}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}

        _write_response(response)


if __name__ == "__main__":
    main()
