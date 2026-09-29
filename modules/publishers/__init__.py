"""
Paket: publishers
Tjänsterna som appen publicerar avsnitt på ("leverantörer"). Resten av
appen pratar med den valda tjänsten via get_publisher(), aldrig direkt med
t.ex. modules/spreaker_client.py - så att fler tjänster kan läggas till
utan att pipelinen eller fliken Avsnitt behöver skrivas om.

Varje tjänst anger vad den klarar (Capabilities). Fliken Avsnitt visar
bara det: kan tjänsten inte uppdatera ett avsnitt visas "📋 Kopiera" i
stället för "💾 Spara", och kan den inte lista avsnitt bygger listan bara
på appens egen historik (modules/episode_library.py).

Just nu finns bara Spreaker (PUBLISH_PROVIDER=spreaker).
"""
import config
from modules.publishers.base import Capabilities, Publisher, PublisherError
from modules.publishers.spreaker import SpreakerPublisher

__all__ = ["Capabilities", "Publisher", "PublisherError", "get_publisher"]

# Tjänsterna som går att välja, efter sitt namn i PUBLISH_PROVIDER.
_PUBLISHERS: dict[str, type[Publisher]] = {
    SpreakerPublisher.key: SpreakerPublisher,
}


def get_publisher() -> Publisher:
    """
    Den tjänst som är vald i inställningarna (PUBLISH_PROVIDER).

    Returns:
        En ny instans - tjänsterna läser sina inställningar ur config vid
        varje anrop, så en ändrad inställning gäller direkt.
    """
    return _PUBLISHERS.get(config.PUBLISH_PROVIDER, SpreakerPublisher)()
