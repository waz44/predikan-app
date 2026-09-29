"""
Modul: publishers.base
Det gemensamma gränssnittet för tjänsterna som appen publicerar på.

En ny tjänst ärver Publisher, fyller i key/label och de metoder den klarar,
och anger det i capabilities(). Metoder för sådant tjänsten inte klarar
behöver inte skrivas - de kastar PublisherError som standard, och fliken
Avsnitt visar dem aldrig (se Capabilities).
"""
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


class PublisherError(Exception):
    """Ett fel från tjänsten, med ett meddelande som kan visas för användaren."""


@dataclass(frozen=True)
class Capabilities:
    """
    Vad tjänsten klarar just nu (med de inställningar som gäller).

    Alla är False när tjänsten inte är inställd eller publiceringen
    simuleras - då finns inget riktigt konto att prata med.
    """

    # Hämta listan med avsnitt som ligger på kontot ("🔄 Hämta från ...").
    list_episodes: bool = False
    # Ändra titel och beskrivning på ett publicerat avsnitt ("💾 Spara").
    update_episode: bool = False
    # Ladda ner ett avsnitts ljud (för "Generera om" utan sparat transkript).
    download_audio: bool = False

    def as_dict(self) -> dict:
        """Som dict, för JSON till webbsidan."""
        return asdict(self)


class Publisher:
    """
    En tjänst som appen publicerar avsnitt på.

    Avsnitts-id:n är tjänstens egna. Spreaker använder heltal; andra
    tjänster använder text - därför hanteras de som text utanför tjänsten.
    """

    # Namnet i PUBLISH_PROVIDER, t.ex. "spreaker".
    key: str = ""
    # Namnet som visas för användaren, t.ex. "Spreaker".
    label: str = ""

    def is_configured(self) -> bool:
        """Om kontot är inställt (nycklar, show m.m.)."""
        return False

    def is_live(self) -> bool:
        """Om avsnitt publiceras på riktigt - inställt och inte simulerat."""
        return False

    def capabilities(self) -> Capabilities:
        """Vad tjänsten klarar just nu (se Capabilities)."""
        return Capabilities()

    def publish_episode(
        self,
        audio_path: Path,
        title: str,
        description: str,
        tags: list[str],
        publish_date: str = "",
        progress_callback: Callable[[int], None] | None = None,
    ) -> dict:
        """
        Laddar upp och publicerar (eller schemalägger/bakåtdaterar) ett avsnitt.

        Returns:
            {"episode_id", "episode_url", "simulated", "scheduled", "backdated"}.
        """
        raise PublisherError(f"{self.label} kan inte publicera avsnitt.")

    def fetch_episodes(self) -> None:
        """Hämtar avsnittslistan från tjänsten och sparar den lokalt."""
        raise PublisherError(f"{self.label} kan inte lista avsnitt.")

    def cached_episodes(self) -> list[dict]:
        """
        Den lokalt sparade avsnittslistan (inga anrop mot tjänsten).

        Returns:
            En dict per avsnitt med minst episode_id, title, description,
            published_at, duration_seconds, plays_count, site_url och
            has_transcript.
        """
        return []

    def cached_episode(self, episode_id: str) -> dict | None:
        """Ett avsnitt ur den lokalt sparade listan, eller None."""
        return None

    def update_episode(self, episode_id: str, title: str, description: str) -> None:
        """Sparar ny titel och beskrivning för ett publicerat avsnitt."""
        raise PublisherError(f"{self.label} kan inte uppdatera avsnitt.")

    def download_audio(self, episode_id: str, dest_path: Path) -> None:
        """Laddar ner ett avsnitts ljud till dest_path."""
        raise PublisherError(f"{self.label} kan inte ladda ner avsnittens ljud.")

    def get_transcript(self, episode_id: str) -> str | None:
        """Ett sparat transkript för avsnittet, eller None."""
        return None

    def save_transcript(self, episode_id: str, transcript: str) -> None:
        """Sparar transkriptet för avsnittet, så att "Generera om" kan återanvända det."""
