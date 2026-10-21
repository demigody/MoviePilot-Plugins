import copy

import pytest


@pytest.fixture(autouse=True)
def isolate_plugin_data(monkeypatch):
    from app.plugins.medialibraryplaylistmanager import MediaLibraryPlaylistManager

    def save_data(self, key, value, plugin_id=None):
        data = self.__dict__.setdefault("_test_plugin_data", {})
        data[key] = copy.deepcopy(value)

    def get_data(self, key=None, plugin_id=None):
        data = self.__dict__.get("_test_plugin_data", {})
        return copy.deepcopy(data if key is None else data.get(key))

    def update_config(self, config, plugin_id=None):
        self._stored_config = copy.deepcopy(config)
        return True

    monkeypatch.setattr(MediaLibraryPlaylistManager, "save_data", save_data)
    monkeypatch.setattr(MediaLibraryPlaylistManager, "get_data", get_data)
    monkeypatch.setattr(MediaLibraryPlaylistManager, "update_config", update_config)
