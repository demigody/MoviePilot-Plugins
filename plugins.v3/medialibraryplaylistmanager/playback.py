"""Selected works own order. Episode expansion exists only during publication."""
from __future__ import annotations

from .emby import EmbyEpisode, EmbyItem, EmbySeason
from .models import MediaMember, playlist_parent


def playlist_content(order: list[str], members: list[MediaMember], matches: dict[str, EmbyItem],
                     episodes: list[EmbyItem], existing: list[EmbyItem]) -> tuple[list[str], dict[str, EmbyItem]]:
    member_keys = [member.key for member in members]
    scopes = {member.key: member.seasons for member in members}
    def selected(item):
        return (isinstance(item, EmbyEpisode) and item.parent_key in scopes
                and (scopes[item.parent_key] is None or item.season_number in scopes[item.parent_key]))
    playable = {key: item for key, item in matches.items() if item.media_type == "movie"}
    playable.update({item.key: item for item in episodes if item.key and selected(item)})
    # Retain already published episodes of a configured show while it is not
    # visible in the selected inventory. New versions still prefer fresh data.
    for item in existing:
        if selected(item) and item.key:
            playable.setdefault(item.key, item)
    # Old configurations may contain season keys. Interpret the first position
    # of each selected work as its whole-work position without changing IDs.
    order = list(dict.fromkeys(playlist_parent(key) for key in order if playlist_parent(key) in member_keys))
    order.extend(key for key in member_keys if key not in order)
    keys = []
    for parent in order:
        if parent.startswith("movie:"):
            keys.append(parent)
            continue
        values = sorted((item for item in playable.values() if isinstance(item, EmbyEpisode) and item.parent_key == parent),
                        key=lambda item: (item.season_number, item.episode_number, item.id))
        keys.extend(item.key for item in values)
    return keys, playable


def collection_content(members: list[MediaMember], matches: dict[str, EmbyItem],
                       seasons: list[EmbyItem], existing: list[EmbyItem]) -> tuple[list[str], dict[str, EmbyItem]]:
    available = dict(matches)
    available.update({item.key: item for item in seasons if item.key})
    keys = []
    for member in members:
        if member.seasons is not None:
            # Never fall back to a Series ID: that would include unselected seasons.
            keys.extend(f"{member.key}:season:{number}" for number in member.seasons)
        else:
            keys.append(member.key)
            if member.key not in matches and not any(item.key == member.key for item in existing):
                keys.extend(item.key for item in existing if isinstance(item, EmbySeason)
                            and item.parent_key == member.key and item.key)
    return list(dict.fromkeys(keys)), available
