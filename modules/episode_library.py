"""
Modul: episode_library
Listan i fliken 📡 Avsnitt: tjänstens avsnitt (t.ex. Spreaker) och appens
egen historik över vad den publicerat, sammanslagna till en lista.

- Avsnitt som finns hos tjänsten visas med tjänstens uppgifter (det som
  faktiskt är publicerat), även sådant som inte publicerats via appen.
- Appens egna publiceringar som inte finns i tjänstens lista - t.ex. när
  tjänsten inte kan lista avsnitt, listan inte hämtats på länge eller
  publiceringen simulerades - visas från historiken. Deras transkript
  finns kvar lokalt, så "Generera om" fungerar för dem också.

Varje avsnitt har ett id som text: tjänstens id när avsnittet finns där
("75397245"), annars "h" + radens id i historiken ("h12").
"""
from modules import episode_store, podcast_archive
from modules.publishers import Publisher, get_publisher

# Prefix för avsnitt som bara finns i appens historik.
HISTORY_PREFIX = "h"


def history_id_of(item_id: str) -> int | None:
    """
    Radens id i historiken för ett id som "h12", annars None.

    Args:
        item_id: Ett id från listan.

    Returns:
        12 för "h12"; None för ett av tjänstens id:n.
    """
    if item_id.startswith(HISTORY_PREFIX) and item_id[len(HISTORY_PREFIX):].isdigit():
        return int(item_id[len(HISTORY_PREFIX):])
    return None


def _archive_info(episode_id: str | None, index: dict) -> dict:
    """Om avsnittet finns i det lokala podd-arkivet (bara Spreaker-flödet har numeriska id:n)."""
    if not episode_id or not episode_id.isdigit():
        return {"archived": False, "has_transcript": False}
    return podcast_archive.local_info(int(episode_id), index)


def list_items(publisher: Publisher | None = None) -> dict:
    """
    Den sammanslagna avsnittslistan för fliken Avsnitt.

    Args:
        publisher: Tjänsten (standard: den som är vald i inställningarna).

    Returns:
        {"provider": {"key", "label", "live", "capabilities"}, "items": [...]}
        där varje avsnitt har id, episode_id (tjänstens id eller None),
        title, description, published_at, duration_seconds, plays_count,
        site_url, archived, has_transcript, on_provider, in_history,
        simulated och editable.
    """
    publisher = publisher or get_publisher()
    caps = publisher.capabilities()
    live = publisher.is_live()
    archive_index = podcast_archive.index()

    items: list[dict] = []
    seen: set[str] = set()

    # 1) Tjänstens avsnitt - det som faktiskt är publicerat.
    if live:
        for ep in publisher.cached_episodes():
            episode_id = str(ep["episode_id"])
            seen.add(episode_id)
            local = _archive_info(episode_id, archive_index)
            items.append({
                "id": episode_id,
                "episode_id": ep["episode_id"],
                "title": ep.get("title") or "",
                "description": ep.get("description") or "",
                "speaker": ep.get("speaker"),
                "published_at": ep.get("published_at"),
                "duration_seconds": ep.get("duration_seconds"),
                "plays_count": ep.get("plays_count"),
                "site_url": ep.get("site_url"),
                "archived": local["archived"],
                "has_transcript": bool(ep.get("has_transcript")) or local["has_transcript"],
                "on_provider": True,
                "in_history": False,
                "simulated": False,
                "editable": caps.update_episode,
            })
    by_id = {item["id"]: item for item in items}

    # 2) Appens egna publiceringar.
    for row in episode_store.list_published():
        provider_id = row.get("provider_episode_id")
        has_file = episode_store.read_transcript(row) is not None
        if provider_id and row.get("provider") == publisher.key and provider_id in by_id:
            # Finns redan hos tjänsten - märk bara att appen publicerade det.
            by_id[provider_id]["in_history"] = True
            by_id[provider_id]["has_transcript"] = by_id[provider_id]["has_transcript"] or has_file
            continue
        simulated = bool(row["simulated"])
        # Simulerade publiceringar visas bara så länge publiceringen
        # simuleras - med ett riktigt konto är de bara skräp i listan.
        if simulated and live:
            continue
        known_on_provider = bool(provider_id) and row.get("provider") == publisher.key and not simulated
        item_id = provider_id if known_on_provider else f"{HISTORY_PREFIX}{row['id']}"
        if item_id in seen:
            continue
        seen.add(item_id)
        local = _archive_info(provider_id if known_on_provider else None, archive_index)
        items.append({
            "id": item_id,
            "episode_id": int(provider_id) if known_on_provider and provider_id.isdigit() else None,
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "speaker": row.get("speaker"),
            # Publiceringsdatum om ett angavs, annars när appen publicerade avsnittet.
            "published_at": row.get("publish_date") or row.get("created_at"),
            "duration_seconds": row.get("sermon_seconds"),
            "plays_count": None,
            "site_url": None if simulated else row.get("episode_url"),
            "archived": local["archived"],
            "has_transcript": has_file or local["has_transcript"],
            "on_provider": False,
            "in_history": True,
            "simulated": simulated,
            # Ett avsnitt som finns hos tjänsten går att uppdatera även om
            # listan inte hämtats sedan det publicerades.
            "editable": caps.update_episode and known_on_provider,
        })

    items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return {
        "provider": {
            "key": publisher.key,
            "label": publisher.label,
            "live": live,
            "capabilities": caps.as_dict(),
        },
        "items": items,
    }
