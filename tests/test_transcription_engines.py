"""
Val av lokal transkriberingsmotor (modules/transcription.py): Whisper
(inklusive svenska KB-Whisper via faster-whisper) eller Pianissimo via
onnx-asr. Modellerna själva stubbas - inga nedladdningar i tester.
"""
import sys
from types import SimpleNamespace

import pytest

import config
from modules import transcription


@pytest.fixture(autouse=True)
def fresh_model_cache(monkeypatch):
    """
    Körs automatiskt före varje test: tom modellcache och lokal transkribering påslagen.
    """
    monkeypatch.setattr(transcription, "_local_model", None)
    monkeypatch.setattr(transcription, "_local_backend", None)
    monkeypatch.setattr(transcription, "_loaded_key", None)
    monkeypatch.setattr(config, "USE_LOCAL_WHISPER", True)


def test_engine_setting_selects_pianissimo_or_whisper(monkeypatch, tmp_path):
    """
    Inställningen LOCAL_ASR_ENGINE avgör vilken motor som används.
    """
    monkeypatch.setattr(transcription, "_transcribe_pianissimo", lambda path: "pianissimo")
    monkeypatch.setattr(transcription, "_transcribe_local", lambda path: "whisper")

    monkeypatch.setattr(config, "LOCAL_ASR_ENGINE", "pianissimo")
    assert transcription.transcribe_audio(tmp_path / "a.mp3") == "pianissimo"
    monkeypatch.setattr(config, "LOCAL_ASR_ENGINE", "whisper")
    assert transcription.transcribe_audio(tmp_path / "a.mp3") == "whisper"


def test_pianissimo_joins_vad_segments(monkeypatch, tmp_path):
    """
    Pianissimos bitar sätts ihop med mellanslag, tomma bitar hoppas över
    och ljudet skickas med 16 kHz.
    """
    received = {}

    class FakeModel:
        def recognize(self, samples, sample_rate):
            received["sample_rate"] = sample_rate
            return iter([
                SimpleNamespace(text=" Du lyssnar på en podcast. "),
                SimpleNamespace(text=""),
                SimpleNamespace(text="Välkommen till gudstjänsten."),
            ])

    monkeypatch.setattr(config, "LOCAL_ASR_ENGINE", "pianissimo")
    monkeypatch.setattr(transcription, "_load_pianissimo", lambda: ("pianissimo", FakeModel()))
    monkeypatch.setattr(transcription, "_read_audio_16k_mono", lambda path: [0.0])

    text = transcription.transcribe_audio(tmp_path / "predikan.mp3")
    assert text == "Du lyssnar på en podcast. Välkommen till gudstjänsten."
    assert received["sample_rate"] == 16_000


def test_model_reloads_only_when_settings_change(monkeypatch):
    """
    Modellen laddas en gång per inställning - samma inställning återanvänder
    den, en ny inställning laddar om.
    """
    loads = []

    def loader():
        loads.append(config.PIANISSIMO_MODEL)
        return "pianissimo", object()

    monkeypatch.setattr(config, "PIANISSIMO_MODEL", "förråd/a")
    transcription._ensure_model(("pianissimo", config.PIANISSIMO_MODEL), loader)
    transcription._ensure_model(("pianissimo", config.PIANISSIMO_MODEL), loader)
    assert loads == ["förråd/a"]  # ingen ny laddning med samma inställning

    monkeypatch.setattr(config, "PIANISSIMO_MODEL", "förråd/b")
    transcription._ensure_model(("pianissimo", config.PIANISSIMO_MODEL), loader)
    assert loads == ["förråd/a", "förråd/b"]


def test_kb_whisper_without_faster_whisper_gives_clear_error(monkeypatch):
    """
    KB-Whisper utan faster-whisper ger ett begripligt fel i stället för ett
    kryptiskt fel från openai-whisper.
    """
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import ger ImportError
    monkeypatch.setattr(config, "LOCAL_WHISPER_MODEL", "KBLab/kb-whisper-small")
    monkeypatch.setattr(config, "WHISPER_DEVICE", "cpu")
    with pytest.raises(RuntimeError, match="kräver faster-whisper"):
        transcription._load_local_model()


def test_pianissimo_without_onnx_asr_gives_clear_error(monkeypatch):
    """
    Pianissimo utan paketet onnx-asr ger ett fel som säger vad som ska installeras.
    """
    monkeypatch.setitem(sys.modules, "onnx_asr", None)
    with pytest.raises(RuntimeError, match="onnx-asr"):
        transcription._load_pianissimo()


def test_setup_accepts_only_known_engines(client, monkeypatch):
    """
    Inställningssidan godtar bara "whisper" och "pianissimo" som motor.
    """
    written = {}
    monkeypatch.setattr("routers.setup.env_file.set_values", lambda updates: written.update(updates))
    monkeypatch.setattr("routers.setup.config.reload", lambda: None)

    assert client.post("/api/setup/save", json={"values": {"LOCAL_ASR_ENGINE": "pianissimo"}}).status_code == 200
    assert written["LOCAL_ASR_ENGINE"] == "pianissimo"
    assert client.post("/api/setup/save", json={"values": {"LOCAL_ASR_ENGINE": "okänd"}}).status_code == 400


def test_pianissimo_config_gets_features_size(tmp_path):
    """onnx-asr läser "features_size" (annars 80) - ONNX-versionen anger 128 som "features"."""
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model_type": "fastconformer_tdt", "features": 128}), encoding="utf-8")
    transcription._fix_pianissimo_config(path)
    assert json.loads(path.read_text(encoding="utf-8"))["features_size"] == 128

    # Redan komplett: rörs inte.
    path.write_text(json.dumps({"features": 128, "features_size": 80}), encoding="utf-8")
    transcription._fix_pianissimo_config(path)
    assert json.loads(path.read_text(encoding="utf-8"))["features_size"] == 80


def test_pianissimo_download_is_skipped_when_files_exist(tmp_path, monkeypatch):
    """
    Finns modellfilerna redan hämtas inget från Hugging Face, men
    config.json kompletteras ändå med features_size.
    """
    import json

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    target = tmp_path / "models" / "pianissimo-sv-onnx"
    target.mkdir(parents=True)
    for name in transcription._PIANISSIMO_FILES:
        (target / name).write_text(json.dumps({"features": 128}) if name == "config.json" else "x", encoding="utf-8")

    def _no_download(*args, **kwargs):
        raise AssertionError("ska inte kontakta Hugging Face när filerna redan finns")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=_no_download))
    assert transcription._download_pianissimo("moonhouse/pianissimo-sv-onnx") == target
    assert json.loads((target / "config.json").read_text(encoding="utf-8"))["features_size"] == 128
