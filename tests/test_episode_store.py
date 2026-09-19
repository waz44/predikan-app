"""Enhetstester för modules/episode_store.py - episodhistorik, statistik och lagringsrensning."""
from pathlib import Path

from modules import episode_store


def _record(base_name: str, sermon_seconds: float, processing_seconds: float, created_at: str, processed_dir: Path):
    audio_path = processed_dir.parent / f"{base_name}-clipped.mp3"
    audio_path.write_text("x")
    for suffix in ("-transcript.txt", "-enrichment.json", "-ai-openai-title.txt"):
        (processed_dir / f"{base_name}{suffix}").write_text("x")

    episode_store.record_episode({
        "base_name": base_name,
        "speaker": "Talare",
        "title": f"Titel {base_name}",
        "description": "Beskrivning",
        "tags": ["Tro & Tvivel"],
        "publish_date": "",
        "category": "",
        "kind": "manual",
        "episode_url": f"https://example.com/{base_name}",
        "simulated": True,
        "scheduled": False,
        "backdated": False,
        "email_sent": False,
        "audio_path": str(audio_path),
        "transcript_path": str(processed_dir / f"{base_name}-transcript.txt"),
        "enrichment_path": str(processed_dir / f"{base_name}-enrichment.json"),
        "sermon_seconds": sermon_seconds,
        "processing_seconds": processing_seconds,
        "created_at": created_at,
    })
    return audio_path


def test_get_stats_empty(tmp_env):
    stats = episode_store.get_stats()
    assert stats == {
        "total_count": 0,
        "total_sermon_seconds": 0,
        "total_processing_seconds": 0,
        "processing_ratio": None,
    }


def test_get_stats_computes_ratio(tmp_env):
    processed_dir = tmp_env["processed_dir"]
    for i in range(3):
        _record(f"ep{i}", sermon_seconds=100.0, processing_seconds=40.0, created_at=f"2026-01-0{i + 1}T00:00:00", processed_dir=processed_dir)

    stats = episode_store.get_stats()
    assert stats["total_count"] == 3
    assert stats["total_sermon_seconds"] == 300.0
    assert stats["total_processing_seconds"] == 120.0
    assert stats["processing_ratio"] == 0.4


def test_estimate_processing_seconds_uses_history_or_none(tmp_env):
    # Ingen historik än - ingen uppskattning kan göras
    assert episode_store.estimate_processing_seconds(100) is None

    _record("ep0", sermon_seconds=100.0, processing_seconds=50.0, created_at="2026-01-01T00:00:00", processed_dir=tmp_env["processed_dir"])

    # Nu finns historik (ratio 0.5)
    assert episode_store.estimate_processing_seconds(100) == 50.0


def test_enforce_retention_keeps_newest_and_deletes_rest(tmp_env):
    processed_dir = tmp_env["processed_dir"]
    audio_paths = [
        _record(f"ep{i}", 100.0, 50.0, f"2026-01-0{i + 1}T00:00:00", processed_dir)
        for i in range(5)
    ]

    removed = episode_store.enforce_retention(processed_dir, max_episodes=2)
    assert set(removed) == {"ep0", "ep1", "ep2"}

    # Filer för de tre äldsta ska vara borta
    for base in ("ep0", "ep1", "ep2"):
        assert not any(processed_dir.glob(f"{base}-*"))
    assert not audio_paths[0].exists()

    # De två senaste ska finnas kvar
    for base in ("ep3", "ep4"):
        assert list(processed_dir.glob(f"{base}-*")), f"filer för {base} borde finnas kvar"
    assert audio_paths[3].exists() and audio_paths[4].exists()

    stats = episode_store.get_stats()
    assert stats["total_count"] == 2


def test_enforce_retention_noop_when_under_limit(tmp_env):
    processed_dir = tmp_env["processed_dir"]
    _record("ep0", 100.0, 50.0, "2026-01-01T00:00:00", processed_dir)

    removed = episode_store.enforce_retention(processed_dir, max_episodes=5)
    assert removed == []


def test_enforce_retention_disabled_when_max_is_zero(tmp_env):
    processed_dir = tmp_env["processed_dir"]
    for i in range(3):
        _record(f"ep{i}", 100.0, 50.0, f"2026-01-0{i + 1}T00:00:00", processed_dir)

    removed = episode_store.enforce_retention(processed_dir, max_episodes=0)
    assert removed == []
    assert episode_store.get_stats()["total_count"] == 3
