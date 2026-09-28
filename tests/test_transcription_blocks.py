"""
Tester för transkribering i block (audio_processor.split_for_transcription +
transcription.transcribe_audio), Groq/OpenAI-anropen med väntan vid
"för många anrop", den lokala reserven (LOCAL_FALLBACK) och Gemini i
AI-berikningen. Inga riktiga nätverksanrop eller modeller.
"""
import subprocess

import httpx
import openai
import pytest

import config
from modules import ai_enrichment, audio_processor, transcription


def test_cut_points_prefer_a_pause_near_the_target():
    # Paus 590-592 (mitt 591) ligger närmast målet 600; 700-701 är för långt bort.
    cuts = audio_processor.choose_cut_points(1500, [(590, 592), (700, 701)], block_seconds=600)
    assert cuts[0] == 591
    # Ingen paus nära nästa mål (591 + 600) -> delas exakt där.
    assert cuts[1] == 1191


def test_cut_points_short_file_and_short_last_block():
    assert audio_processor.choose_cut_points(500, [], block_seconds=600) == []
    # 630 s: ett sista block på 30 s slås ihop med det första.
    assert audio_processor.choose_cut_points(630, [], block_seconds=600) == []
    assert audio_processor.choose_cut_points(700, [], block_seconds=600) == [600]


def test_split_cuts_in_pauses_and_makes_small_mono_blocks(tmp_path, monkeypatch):
    # 30 s ton + 1 s tystnad, om och om igen, i 185 s. Block om ca 60 s.
    audio = tmp_path / "predikan.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "aevalsrc='if(lt(mod(t,31),30),0.3*sin(2*PI*440*t),0)':d=185:s=44100",
            "-ac", "2", str(audio),
        ],
        check=True,
    )
    monkeypatch.setattr(audio_processor, "TRANSCRIPTION_BLOCK_SECONDS", 60)

    blocks = audio_processor.split_for_transcription(audio, tmp_path / "block", codec="mp3")

    assert len(blocks) == 3
    lengths = [audio_processor.get_audio_duration_seconds(b) for b in blocks]
    # Första delningen mitt i pausen 61-62 s.
    assert lengths[0] == pytest.approx(61.5, abs=0.1)
    assert sum(lengths) == pytest.approx(185, abs=0.3)
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(blocks[0])],
        capture_output=True, text=True,
    ).stdout.strip()
    assert info == "16000,1"


@pytest.fixture
def three_blocks(tmp_path, monkeypatch):
    """Låtsas att ljudet delas i tre block (inga riktiga ljudfiler)."""
    blocks = [tmp_path / f"block-{i}.mp3" for i in (1, 2, 3)]
    monkeypatch.setattr(transcription.audio_processor, "split_for_transcription", lambda path, out_dir, codec: blocks)
    return blocks


def test_blocks_are_joined_with_context_and_progress(three_blocks, monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPTION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk-test")
    calls = []

    def fake_api(block, api_key, model, context, base_url=None):
        calls.append((block.name, model, context, base_url))
        return f" Text {len(calls)}. "

    monkeypatch.setattr(transcription, "_transcribe_api", fake_api)
    progress = []

    text = transcription.transcribe_audio(three_blocks[0], on_progress=lambda d, t: progress.append((d, t)))

    assert text == "Text 1. Text 2. Text 3."
    assert progress == [(1, 3), (2, 3), (3, 3)]
    # Groqs adress och modell, och föregående blocks text som sammanhang.
    assert calls[0] == ("block-1.mp3", "whisper-large-v3", "", "https://api.groq.com/openai/v1")
    assert calls[1][2] == "Text 1."


def test_local_fallback_only_when_enabled(three_blocks, monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPTION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk-test")
    monkeypatch.setattr(config, "LOCAL_ASR_ENGINE", "whisper")

    def failing_api(*args, **kwargs):
        raise RuntimeError("gratiskvoten slut")

    monkeypatch.setattr(transcription, "_transcribe_api", failing_api)
    monkeypatch.setattr(transcription, "_transcribe_local", lambda path, context="": "lokalt")

    monkeypatch.setattr(config, "LOCAL_FALLBACK", False)
    with pytest.raises(RuntimeError, match="gratiskvoten"):
        transcription.transcribe_audio(three_blocks[0])

    monkeypatch.setattr(config, "LOCAL_FALLBACK", True)
    assert transcription.transcribe_audio(three_blocks[0]) == "lokalt lokalt lokalt"


def test_api_waits_and_retries_on_rate_limit(tmp_path, monkeypatch):
    block = tmp_path / "block-001.mp3"
    block.write_bytes(b"ljud")
    attempts = []

    class FakeTranscriptions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                response = httpx.Response(
                    429, headers={"retry-after": "7"}, request=httpx.Request("POST", "https://api.groq.com"),
                )
                raise openai.RateLimitError("för många anrop", response=response, body=None)
            return type("Result", (), {"text": "Hej"})()

    class FakeClient:
        def __init__(self, api_key, base_url=None):
            self.audio = type("Audio", (), {"transcriptions": FakeTranscriptions()})()

    slept = []
    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(transcription.time, "sleep", lambda s: slept.append(s))

    assert transcription._transcribe_api(block, "gsk", "whisper-large-v3", "förra", "https://x") == "Hej"
    assert slept == [7.0]
    assert attempts[1]["language"] == "sv" and attempts[1]["prompt"] == "förra"


def test_groq_without_key_gives_clear_error(three_blocks, monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPTION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "LOCAL_FALLBACK", False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY saknas"):
        transcription.transcribe_audio(three_blocks[0])


def test_old_env_without_provider_keeps_working(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_PROVIDER", raising=False)
    monkeypatch.setenv("USE_LOCAL_WHISPER", "true")
    assert config._transcription_provider() == "local"
    monkeypatch.setenv("USE_LOCAL_WHISPER", "false")
    assert config._transcription_provider() == "openai"
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "groq")
    assert config._transcription_provider() == "groq"


def test_gemini_is_used_and_falls_back_to_ollama(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(ai_enrichment, "_call_gemini", lambda prompt, temperature: "Anna: Från Gemini")
    assert ai_enrichment.generate_title("Transkript.", "Anna") == "Anna: Från Gemini"

    def failing_gemini(prompt, temperature):
        raise RuntimeError("429")

    monkeypatch.setattr(ai_enrichment, "_call_gemini", failing_gemini)
    monkeypatch.setattr(ai_enrichment, "_call_ollama", lambda prompt, temperature: "Anna: Från Ollama")

    monkeypatch.setattr(config, "LOCAL_FALLBACK", False)
    with pytest.raises(RuntimeError, match="429"):
        ai_enrichment.generate_title("Transkript.", "Anna")

    monkeypatch.setattr(config, "LOCAL_FALLBACK", True)
    assert ai_enrichment.generate_title("Transkript.", "Anna") == "Anna: Från Ollama"


def test_gemini_call_uses_googles_openai_endpoint(monkeypatch):
    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            message = type("M", (), {"content": "svar"})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    class FakeClient:
        def __init__(self, api_key, base_url=None):
            seen["base_url"] = base_url
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-test")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-3.8-flash")

    assert ai_enrichment._call_gemini("prompt", 0.2) == "svar"
    assert seen["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert seen["model"] == "gemini-3.8-flash"
    # Inget tak på svarets längd - Gemini 3 räknar in sitt "tänkande" i det.
    assert "max_tokens" not in seen
