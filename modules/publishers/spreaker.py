"""
Modul: publishers.spreaker
Spreaker som tjänst att publicera på. Ett tunt lager ovanpå
modules/spreaker_client.py (anropen mot Spreakers API) och
modules/spreaker_episode_store.py (den lokala avsnittslistan och de
sparade transkripten).

Spreakers avsnitts-id:n är heltal; utåt hanteras de som text (se
publishers.base) och görs om här.
"""
from collections.abc import Callable
from pathlib import Path

import config
from modules import spreaker_client, spreaker_episode_store
from modules.publishers.base import Capabilities, Publisher, PublisherError


class SpreakerPublisher(Publisher):
    """Publicering och avsnittshantering mot Spreaker."""

    key = "spreaker"
    label = "Spreaker"

    def is_configured(self) -> bool:
        return bool(config.SPREAKER_API_TOKEN) and bool(config.SPREAKER_SHOW_ID)

    def is_live(self) -> bool:
        return self.is_configured() and not config.SPREAKER_SIMULATE

    def capabilities(self) -> Capabilities:
        # Spreaker klarar allt - men bara med ett riktigt konto. Det finns
        # inget meningsfullt simulerat läge för att ändra publicerade avsnitt.
        live = self.is_live()
        return Capabilities(list_episodes=live, update_episode=live, download_audio=live)

    def publish_episode(
        self,
        audio_path: Path,
        title: str,
        description: str,
        tags: list[str],
        publish_date: str = "",
        progress_callback: Callable[[int], None] | None = None,
    ) -> dict:
        # Med SPREAKER_SIMULATE=true simuleras uppladdningen (inget skickas).
        return spreaker_client.publish_episode(
            audio_path=audio_path,
            title=title,
            description=description,
            tags=tags,
            publish_date=publish_date,
            progress_callback=progress_callback,
        )

    def fetch_episodes(self) -> None:
        try:
            raw_episodes = spreaker_client.list_episodes()
        except spreaker_client.SpreakerUploadError as exc:
            raise PublisherError(str(exc)) from exc
        spreaker_episode_store.replace_all(raw_episodes)

    def cached_episodes(self) -> list[dict]:
        return spreaker_episode_store.get_all()

    def cached_episode(self, episode_id: str) -> dict | None:
        return spreaker_episode_store.get(int(episode_id))

    def update_episode(self, episode_id: str, title: str, description: str) -> None:
        try:
            spreaker_client.update_episode(int(episode_id), title, description)
        except spreaker_client.SpreakerUploadError as exc:
            raise PublisherError(str(exc)) from exc
        # Den lokala listan uppdateras först när Spreaker har tagit emot ändringen.
        spreaker_episode_store.update_local(int(episode_id), title, description)

    def download_audio(self, episode_id: str, dest_path: Path) -> None:
        try:
            spreaker_client.download_episode_audio(int(episode_id), dest_path)
        except spreaker_client.SpreakerUploadError as exc:
            raise PublisherError(str(exc)) from exc

    def get_transcript(self, episode_id: str) -> str | None:
        return spreaker_episode_store.get_transcript(int(episode_id))

    def save_transcript(self, episode_id: str, transcript: str) -> None:
        spreaker_episode_store.save_transcript(int(episode_id), transcript)
