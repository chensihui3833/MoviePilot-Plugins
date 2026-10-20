import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeBase:
    def __init__(self):
        self.saved = {}
        self.config = {}

    def update_config(self, config):
        self.config = dict(config)

    def save_data(self, key, value):
        self.saved[key] = value

    def get_data(self, key):
        return self.saved.get(key)


fake_sdk = types.ModuleType("app.sdk.plugin")
fake_sdk._PluginBase = FakeBase
spec = importlib.util.spec_from_file_location(
    "blurayepisodes", Path(__file__).parents[1] / "plugins/blurayepisodes/__init__.py",
    submodule_search_locations=[str(Path(__file__).parents[1] / "plugins/blurayepisodes")],
)
adapter = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"app.sdk.plugin": fake_sdk, "blurayepisodes": adapter}):
    spec.loader.exec_module(adapter)


class AdapterTest(unittest.TestCase):
    def test_toggle_does_not_start_work(self):
        plugin = adapter.BlurayEpisodes()
        with patch.object(adapter, "native_transfer") as transfer:
            plugin.init_plugin({"enabled": True, "run_once": True})
            self.assertTrue(plugin.get_state())
            transfer.assert_not_called()
            plugin.init_plugin({"enabled": False})
            self.assertFalse(plugin.get_state())

    def test_public_provider_is_registered_and_forwards_preview(self):
        plugin = adapter.BlurayEpisodes()
        plugin.init_plugin({"enabled": True})
        provider = plugin.get_module()["transfer"]
        with patch.object(adapter, "native_transfer", return_value="result") as transfer:
            self.assertEqual(provider("file", "meta", "media", preview=True), "result")
            self.assertTrue(transfer.call_args.args[-1])
        plugin.stop_service()
        self.assertFalse(plugin.get_state())

    def test_form_only_requires_enable(self):
        plugin = adapter.BlurayEpisodes()
        self.assertEqual(plugin.get_form()[1], {"enabled": False})


if __name__ == "__main__":
    unittest.main()
