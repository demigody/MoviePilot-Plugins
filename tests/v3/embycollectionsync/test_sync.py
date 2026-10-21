"""Transport integration tests: exercise actual REST adapter and publisher."""
import copy

import pytest

from app.plugins.embycollectionsync.emby import EmbyClient
from app.plugins.embycollectionsync.models import CollectionBinding, MediaList, MediaMember, Playlist, Target
from app.plugins.embycollectionsync.sync import Publisher


def media(identifier, tmdb, kind="Movie"):
    return {"Id": identifier, "Name": identifier, "Type": kind, "ProviderIds": {"Tmdb": str(tmdb)}}


class RestServer:
    """A stateful Emby protocol double, not a real Emby server."""

    def __init__(self):
        self.media = {"m1": media("m1", 1), "m2": media("m2", 2), "m3": media("m3", 3), "s1": media("s1", 1, "Series")}
        self.inventory = ["m1", "m2"]
        self.collections = {"c": ["m3", "m1"]}
        self.playlists = {"p": [("e3", "m3"), ("e1", "m1")]}
        self.collection_names = {"c": "Existing collection"}
        self.playlist_names = {"p": "Existing playlist"}
        self.calls = []
        self.fail = None
        self.hook = None
        self.next_entry = 10
        self.episodes = {}
        self.seasons = {}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def client(self):
        return EmbyClient("http://server", "private", "u", self, page_size=1)

    def request(self, method, path, params=None, **kwargs):
        route = path.removeprefix("emby/").split("/")
        action = ""
        object_id = route[1] if len(route) > 1 else None
        if route[0] == "Shows":
            action = "seasons" if route[2] == "Seasons" else "episodes"
        elif route[0] == "Users" and len(route) == 4:
            action, object_id = "get_output", route[3]
        elif route[0] == "Users" and params.get("IncludeItemTypes") in {"BoxSet", "Playlist"}:
            action = "catalog_collection" if params["IncludeItemTypes"] == "BoxSet" else "catalog_playlist"
        elif route[0] == "Users":
            action = "get_collection" if params.get("ParentId") else "inventory"
        elif route[0] == "Collections":
            action = "create_collection" if len(route) == 1 else "add_collection" if method == "POST" else "remove_collection"
        elif route[0] == "Playlists":
            action = "create_playlist" if len(route) == 1 else "get_playlist" if method == "GET" else "move" if "Move" in route else "add_playlist" if method == "POST" else "remove_playlist"
        self.calls.append((method, action, copy.deepcopy(params)))
        if self.hook:
            self.hook(action, object_id)
        if action == self.fail:
            return 503, {}
        if action == "get_output":
            if object_id in self.media:
                return copy.deepcopy(self.media[object_id])
            if object_id in self.collections:
                return {"Id": object_id, "Type": "BoxSet"}
            if object_id in self.playlists:
                return {"Id": object_id, "Type": "Playlist"}
            return 404, {}
        if action in {"episodes", "seasons"}:
            rows = [copy.deepcopy(self.media[item]) for item in (self.seasons if action == 'seasons' else self.episodes).get(object_id, [])]
        elif action == "catalog_collection":
            rows = [{"Id": identifier, "Type": "BoxSet", "Name": self.collection_names.get(identifier, identifier)} for identifier in self.collections]
        elif action == "catalog_playlist":
            rows = [{"Id": identifier, "Type": "Playlist", "Name": self.playlist_names.get(identifier, identifier)} for identifier in self.playlists]
        elif action == "inventory":
            rows = [copy.deepcopy(self.media[item]) for item in self.inventory]
        elif action == "get_collection":
            rows = [copy.deepcopy(self.media[item]) for item in self.collections[params["ParentId"]]]
        elif action == "get_playlist":
            rows = [{**copy.deepcopy(self.media[item]), "PlaylistItemId": entry} for entry, item in self.playlists[object_id]]
        elif action in {"create_collection", "create_playlist"}:
            identifier = "newc" + str(len(self.collections)) if action == "create_collection" else "newp" + str(len(self.playlists))
            ids = params.get("Ids", "").split(",") if params.get("Ids") else []
            if action == "create_collection":
                self.collections[identifier] = list(ids)
                self.collection_names[identifier] = params["Name"]
            else:
                self.playlists[identifier] = []
                self.playlist_names[identifier] = params["Name"]
                self._add_entries(identifier, ids)
            return {"Id": identifier}
        elif action == "add_collection":
            self.collections[object_id].extend(item for item in params["Ids"].split(",") if item not in self.collections[object_id])
            return 204, b""
        elif action == "remove_collection":
            self.collections[object_id] = [item for item in self.collections[object_id] if item not in params["Ids"].split(",")]
            return 204, b""
        elif action == "add_playlist":
            self._add_entries(object_id, params["Ids"].split(","))
            return {"ItemAddedCount": len(params["Ids"].split(","))}
        elif action == "remove_playlist":
            self.playlists[object_id] = [(entry, item) for entry, item in self.playlists[object_id] if entry not in params["EntryIds"].split(",")]
            return 204, b""
        elif action == "move":
            entry = route[3]
            index = int(route[5])
            row = next(item for item in self.playlists[object_id] if item[0] == entry)
            self.playlists[object_id].remove(row)
            self.playlists[object_id].insert(index, row)
            return 204, b""
        else:
            raise AssertionError(f"Unexpected protocol call: {method} {path}")
        start, limit = params["StartIndex"], params["Limit"]
        return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}

    def _add_entries(self, playlist, ids):
        for item in ids:
            self.next_entry += 1
            self.playlists[playlist].append((f"e{self.next_entry}", item))


def listing(*, bound=True, members=None, playlist=True, collection=True):
    target = Target("Emby", collection=CollectionBinding(collection, remote_id="c" if bound else None), playlists=[Playlist("Order", id="localp", remote_id="p" if bound else None, order=["movie:2", "movie:1"])] if playlist else [])
    return MediaList("List", id="local", members=members if members is not None else [MediaMember(1), MediaMember(2)], targets=[target])


def run(server, value, **kwargs):
    return Publisher(server.client()).publish(value, value.targets[0], **kwargs)


def test_ordered_idempotent_publish_verifies_every_output_before_any_removal():
    server = RestServer()
    value = listing()
    result = run(server, value)
    assert result["success"], result
    assert server.collections["c"] == ["m1", "m2"]
    assert [item for _, item in server.playlists["p"]] == ["m2", "m1"]
    actions = [call[1] for call in server.calls]
    first_remove = min(i for i, action in enumerate(actions) if action.startswith("remove_"))
    assert max(i for i, action in enumerate(actions) if action == "move") < first_remove
    assert "add_collection" in actions[:first_remove] and "add_playlist" in actions[:first_remove]
    assert "get_collection" in actions[:first_remove] and actions[:first_remove].count("get_playlist") >= 4
    writes = len(server.writes)
    assert run(server, value)["success"]
    assert len(server.writes) == writes
    assert not any(action == "delete_playlist" for _, action, _ in server.calls)


def test_movie_and_series_same_tmdb_id_match_independently_in_collection():
    server = RestServer()
    server.inventory = ["m1", "s1"]
    value = listing(members=[MediaMember(1), MediaMember(1, "tv")], playlist=False)
    result = run(server, value)
    assert result["success"] and result["matched_count"] == 2
    assert set(server.collections["c"]) == {"m1", "s1"}
    assert result["pending"] == []


def test_unsupported_adapter_playlist_is_rejected_before_read_or_write():
    server = RestServer()
    value = listing(members=[MediaMember(1), MediaMember(1, "tv")])
    client = server.client()
    client.capabilities = {**client.capabilities, 'playlist_media_types': ['movie']}
    result = Publisher(client).publish(value, value.targets[0])
    assert result["error_code"] == "unsupported"
    assert server.calls == []


@pytest.mark.parametrize("failure", ["inventory", "get_playlist", "get_collection", "get_output"])
def test_initial_read_failure_prevents_every_write(failure):
    server = RestServer()
    before = (copy.deepcopy(server.collections), copy.deepcopy(server.playlists))
    server.fail = failure
    result = run(server, listing())
    assert not result["success"]
    assert server.writes == []
    assert (server.collections, server.playlists) == before


@pytest.mark.parametrize("missing", ["collection", "playlist", "both"])
def test_deleted_output_preview_is_read_only_and_formal_publish_rebuilds_once(missing):
    server = RestServer()
    if missing in {"collection", "both"}:
        server.collections.pop("c")
    if missing in {"playlist", "both"}:
        server.playlists.pop("p")
    value = listing()
    before_config = value.to_dict()
    before_server = (copy.deepcopy(server.collections), copy.deepcopy(server.playlists))
    result = run(server, value, dry_run=True)
    assert result["success"], result
    assert any(plan["create"] and plan.get("missing_remote_id") for plan in result["plan"])
    assert server.writes == []
    assert value.to_dict() == before_config
    assert (server.collections, server.playlists) == before_server
    snapshots = []
    assert run(server, value, on_binding=lambda: snapshots.append(copy.deepcopy(value.to_dict())))["success"]
    rebuilt = value.to_dict()
    assert len(snapshots) == (2 if missing == "both" else 1)
    assert value.targets[0].collection.remote_id != "c" if missing in {"collection", "both"} else value.targets[0].collection.remote_id == "c"
    assert value.targets[0].playlists[0].remote_id != "p" if missing in {"playlist", "both"} else value.targets[0].playlists[0].remote_id == "p"
    creates = len([action for _, action, _ in server.writes if action.startswith("create")])
    assert run(server, value)["success"]
    assert value.to_dict() == rebuilt
    assert len([action for _, action, _ in server.writes if action.startswith("create")]) == creates


@pytest.mark.parametrize("kind", ["collection", "playlist"])
def test_new_output_name_collision_blocks_all_target_writes(kind):
    server = RestServer()
    names = server.collection_names if kind == "collection" else server.playlist_names
    names["c" if kind == "collection" else "p"] = " list " if kind == "collection" else " ORDER "
    value = listing(bound=False)
    before = (copy.deepcopy(value.to_dict()), copy.deepcopy(server.collections), copy.deepcopy(server.playlists))
    result = run(server, value)
    assert result["error_code"] == "name_conflict"
    assert "同名" in result["error"] and "修改名称" in result["error"]
    assert server.writes == []
    assert before == (value.to_dict(), server.collections, server.playlists)


def test_two_new_playlists_cannot_claim_same_name_in_one_plan():
    server = RestServer()
    value = listing(bound=False)
    value.targets[0].playlists.append(Playlist(" order "))
    result = run(server, value)
    assert result["error_code"] == "name_conflict"
    assert server.writes == []


def test_bound_output_does_not_conflict_with_its_existing_name():
    server = RestServer()
    server.collection_names["c"] = "List"
    server.playlist_names["p"] = "Order"
    assert run(server, listing())["success"]
    assert not any(action.startswith("create") for _, action, _ in server.writes)


def test_output_catalog_failure_stops_creation_before_any_other_output_write():
    server = RestServer()
    server.fail = "catalog_playlist"
    result = run(server, listing(bound=False))
    assert not result["success"] and result["error_code"] == "server_error"
    assert server.writes == []


@pytest.mark.parametrize("kind", ["collection", "playlist"])
def test_failed_deleted_output_recreation_retains_old_binding(kind):
    server = RestServer()
    (server.collections if kind == "collection" else server.playlists).pop("c" if kind == "collection" else "p")
    server.fail = "create_" + kind
    value = listing()
    before = value.to_dict()
    result = run(server, value)
    assert not result["success"]
    assert value.to_dict() == before
    assert not any(action.startswith("remove") for _, action, _ in server.writes)


@pytest.mark.parametrize("kind", ["collection", "playlist"])
def test_deleted_output_is_not_rebuilt_over_another_same_named_object(kind):
    server = RestServer()
    if kind == "collection":
        server.collections.pop("c")
        server.collections["manual"] = ["m3"]
        server.collection_names["manual"] = "List"
    else:
        server.playlists.pop("p")
        server.playlists["manual"] = [("manual-entry", "m3")]
        server.playlist_names["manual"] = "Order"
    value = listing()
    before = (copy.deepcopy(value.to_dict()), copy.deepcopy(server.collections), copy.deepcopy(server.playlists))
    result = run(server, value)
    assert result["error_code"] == "name_conflict" and "manual" in result["error"]
    assert server.writes == []
    assert before == (value.to_dict(), server.collections, server.playlists)


@pytest.mark.parametrize("failure", ["add_playlist", "add_collection", "move"])
def test_add_or_move_failure_never_removes_from_any_output(failure):
    server = RestServer()
    server.fail = failure
    result = run(server, listing())
    assert not result["success"]
    assert not any(action.startswith("remove") for _, action, _ in server.writes)
    assert "m3" in server.collections["c"]
    assert "m3" in [item for _, item in server.playlists["p"]]


def test_success_response_without_addition_does_not_authorize_removal():
    server = RestServer()
    original = server._add_entries
    server._add_entries = lambda *args: None
    result = run(server, listing())
    assert not result["success"]
    assert not any(action.startswith("remove") for _, action, _ in server.writes)
    server._add_entries = original


def test_success_response_without_move_does_not_authorize_removal():
    server = RestServer()
    original = server.request

    def request(method, path, **kwargs):
        if "/Move/" in path:
            server.calls.append((method, "move", kwargs.get("params")))
            return 204, b""
        return original(method, path, **kwargs)

    server.request = request
    result = run(server, listing())
    assert not result["success"]
    assert not any(action.startswith("remove") for _, action, _ in server.writes)


def test_no_matches_or_default_empty_never_clears_published_objects():
    server = RestServer()
    server.inventory = []
    value = listing()
    result = run(server, value, allow_empty=True)
    assert result["error_code"] == "no_matches"
    assert server.writes == []
    value = listing(members=[])
    result = run(server, value)
    assert result["error_code"] == "invalid_input"
    assert server.writes == []


def test_explicit_empty_clears_bound_contents_and_never_creates_empty_object():
    server = RestServer()
    result = run(server, listing(members=[]), allow_empty=True)
    assert result["success"], result
    assert server.collections["c"] == [] and server.playlists["p"] == []
    assert run(server, listing(bound=False, members=[]), allow_empty=True)["success"]
    assert not any(action.startswith("create") for _, action, _ in server.writes)


def test_pending_member_already_in_remote_output_is_retained():
    server = RestServer()
    server.inventory = ["m1"]
    server.collections["c"] = ["m1", "m2", "m3"]
    server.playlists["p"] = [("e2", "m2"), ("e1", "m1"), ("e3", "m3")]
    result = run(server, listing())
    assert result["success"] and result["pending"] == ["movie:2"]
    assert "m2" in server.collections["c"]
    assert [item for _, item in server.playlists["p"]] == ["m2", "m1"]


def test_unidentified_manual_member_is_preserved_during_normal_publish():
    server = RestServer()
    server.media["manual"] = {"Id": "manual", "Name": "Manual", "Type": "Movie"}
    server.collections["c"].append("manual")
    server.playlists["p"].append(("emanual", "manual"))
    assert run(server, listing())["success"]
    assert "manual" in server.collections["c"] and "manual" in [item for _, item in server.playlists["p"]]


def test_unidentified_repeated_manual_playlist_entries_are_preserved():
    server = RestServer()
    server.media["manual"] = {"Id": "manual", "Name": "Manual", "Type": "Movie"}
    server.playlists["p"].extend([("emanual1", "manual"), ("emanual2", "manual")])
    result = run(server, listing())
    assert result["success"], result
    assert [item for _, item in server.playlists["p"]] == ["m2", "m1", "manual", "manual"]
    assert {entry for entry, item in server.playlists["p"] if item == "manual"} == {"emanual1", "emanual2"}


def test_duplicate_playlist_entry_removed_only_after_keeper_is_verified():
    server = RestServer()
    server.playlists["p"].append(("edup", "m1"))
    assert run(server, listing())["success"]
    assert [item for _, item in server.playlists["p"]] == ["m2", "m1"]


def test_dry_run_returns_exact_plan_without_config_remote_or_callback_mutation():
    server = RestServer()
    value = listing(bound=False)
    before = (copy.deepcopy(value.to_dict()), copy.deepcopy(server.collections), copy.deepcopy(server.playlists))
    calls = []
    result = run(server, value, dry_run=True, on_binding=lambda: calls.append(1))
    assert result["success"] and all(plan["create"] for plan in result["plan"])
    assert before == (value.to_dict(), server.collections, server.playlists)
    assert server.writes == [] and calls == []


def test_new_remote_bindings_persist_before_later_failure_and_are_reused():
    server = RestServer()
    value = listing(bound=False)
    snapshots = []
    server.fail = "get_playlist"
    result = run(server, value, on_binding=lambda: snapshots.append(copy.deepcopy(value.to_dict())))
    assert result["status"] == "partial"
    assert snapshots[0]["targets"][0]["collection"]["remote_id"]
    assert snapshots[-1]["targets"][0]["playlists"][0]["remote_id"]
    original_ids = (value.targets[0].collection.remote_id, value.targets[0].playlists[0].remote_id)
    creates = len([a for _, a, _ in server.writes if a.startswith("create")])
    server.fail = None
    assert run(server, value)["success"]
    assert (value.targets[0].collection.remote_id, value.targets[0].playlists[0].remote_id) == original_ids
    assert len([a for _, a, _ in server.writes if a.startswith("create")]) == creates


def test_binding_persistence_failure_stops_later_writes_and_retains_new_id():
    server = RestServer()
    value = listing(bound=False)

    def fail():
        raise OSError("storage failed")

    result = run(server, value, on_binding=fail)
    assert not result["success"] and "绑定信息保存失败" in result["error"]
    assert value.targets[0].collection.remote_id
    assert [a for _, a, _ in server.writes] == ["create_collection"]


def test_cancel_during_create_still_persists_returned_binding_and_stops():
    server = RestServer()
    value = listing(bound=False)
    state = {"cancelled": False, "saved": False}
    server.hook = lambda action, _: state.update(cancelled=True) if action == "create_collection" else None
    result = run(server, value, cancelled=lambda: state["cancelled"], on_binding=lambda: state.update(saved=True))
    assert result["status"] == "cancelled" and state["saved"]
    assert value.targets[0].collection.remote_id
    assert [a for _, a, _ in server.writes] == ["create_collection"]


def test_deadline_or_preexisting_cancellation_stops_before_network(monkeypatch):
    server = RestServer()
    value = listing()
    assert run(server, value, cancelled=lambda: True)["status"] == "cancelled"
    assert server.calls == []
    clock = iter([0, 1])
    monkeypatch.setattr("app.plugins.embycollectionsync.sync.time.monotonic", lambda: next(clock))
    result = Publisher(server.client(), timeout=0.1).publish(value, value.targets[0])
    assert result["error_code"] == "timeout" and server.calls == []


def test_concurrent_identity_change_prevents_removal():
    server = RestServer()
    state = {"read": 0}

    def hook(action, _):
        if action == "get_collection":
            state["read"] += 1
            if state["read"] == 3:
                server.media["m3"]["ProviderIds"]["Tmdb"] = "4"

    server.hook = hook
    result = run(server, listing())
    assert not result["success"]
    assert not any(action.startswith("remove") for _, action, _ in server.writes)


@pytest.mark.parametrize("change", ["playlist_identity", "collection_identity", "collection_membership"])
def test_change_during_sort_prevents_every_removal(change):
    server = RestServer()
    changed = {"once": False}

    def hook(action, _):
        if action == "move" and not changed["once"]:
            changed["once"] = True
            if change == "playlist_identity":
                server.media["m3"]["ProviderIds"]["Tmdb"] = "4"
            elif change == "collection_identity":
                server.media["manual"] = media("manual", 4)
                server.collections["c"][0] = "manual"
            else:
                server.collections["c"].append("s1")

    server.hook = hook
    result = run(server, listing())
    assert not result["success"]
    assert not any(action.startswith("remove") for _, action, _ in server.writes)


@pytest.mark.parametrize("flag", ["false", 1, None])
def test_destructive_flag_must_be_boolean(flag):
    server = RestServer()
    result = run(server, listing(members=[]), allow_empty=flag)
    assert result["error_code"] == "invalid_input" and server.calls == []
