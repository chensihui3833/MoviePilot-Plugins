import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins.v3/qualitypriorityfix/__init__.py"


class QualityFixTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        source = Path(self.temp.name) / "scan.py"
        source.write_text("class SearchScan:\n"
                          "    def _prepared_batch(self, contexts, ready): return contexts, ready\n"
                          "    def _filter(self, contexts): return contexts\n"
                          "    def _coverage(self, key): return {key}\n")
        scan_module = types.ModuleType("app.chain.search.scan")
        scan_module.__file__ = str(source)

        class SearchScan:
            def _prepared_batch(self, contexts, ready):
                return contexts, {"old-goal"}

        SearchScan._prepared_batch.__module__ = scan_module.__name__
        SearchScan._prepared_batch.__qualname__ = "SearchScan._prepared_batch"
        scan_module.SearchScan = SearchScan
        modules = {name: types.ModuleType(name) for name in (
            "app", "app.chain", "app.chain.search", "app.runtime", "app.runtime.log",
            "app.runtime.version", "app.sdk", "app.sdk.plugin", "app.sdk.plugin.base")}
        modules["app.chain.search"].scan = scan_module
        modules["app.chain.search.scan"] = scan_module
        modules["app.runtime.log"].logger = Mock()
        modules["app.runtime.version"].get_app_version = lambda: "v3.2.2"
        modules["app.sdk.plugin.base"]._PluginBase = object
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        spec = importlib.util.spec_from_file_location("quality_plugin_test", PLUGIN_PATH)
        self.plugin_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin_module)
        self.plugin_module.SUPPORTED_METHOD_SHA256 = self.plugin_module._method_digest(source)
        self.source = source
        self.scan = scan_module.SearchScan
        self.original = self.scan._prepared_batch
        self.plugin = self.plugin_module.QualityPriorityFix()
        self.plugin.systemmessage = Mock()

    def tearDown(self):
        self.plugin.stop_service()
        self.modules.stop()
        self.temp.cleanup()

    def test_catalog_version_matches_plugin(self):
        info = json.loads((ROOT / "package.v3.json").read_text())["QualityPriorityFix"]
        self.assertEqual(info["version"], self.plugin.plugin_version)
        self.assertTrue(info["v3"])
        self.assertFalse(info["v2"])

    def test_refilters_batch_and_rebuilds_goals(self):
        self.plugin.init_plugin({"enabled": True})
        self.assertTrue(self.plugin.get_state())
        scan = self.scan()
        scan._filter = lambda items: sorted((x for x in items if x.valid), key=lambda x: -x.score)
        scan._coverage = lambda key: {key}
        def candidate(key, score, valid=True):
            return types.SimpleNamespace(torrent_info=types.SimpleNamespace(search_resource_id=key),
                                         score=score, valid=valid)
        result, goals = scan._prepared_batch([candidate("low", 1), candidate("high", 9),
                                             candidate("invalid", 10, False)], set())
        self.assertEqual([x.torrent_info.search_resource_id for x in result], ["high", "low"])
        self.assertEqual(goals, {"high", "low"})
        self.plugin.stop_service()
        self.assertIs(self.scan._prepared_batch, self.original)

    def test_changed_critical_method_is_rejected_and_warns(self):
        self.source.write_text(self.source.read_text().replace('return contexts, ready', 'return [], ready'))
        self.plugin.init_plugin({"enabled": True})
        self.assertFalse(self.plugin.get_state())
        self.assertIs(self.scan._prepared_batch, self.original)
        self.plugin.systemmessage.put.assert_called_once()
        self.assertEqual(self.plugin.systemmessage.put.call_args.kwargs["level"], "warning")

    def test_other_code_changes_do_not_block_compatible_methods(self):
        self.source.write_text(self.source.read_text() + "\n# unrelated upgrade\n")
        self.plugin.init_plugin({"enabled": True})
        self.assertTrue(self.plugin.get_state())

    def test_changed_function_is_not_overwritten(self):
        def other(*args):
            return args
        self.scan._prepared_batch = other
        self.plugin.init_plugin({"enabled": True})
        self.assertFalse(self.plugin.get_state())
        self.assertIs(self.scan._prepared_batch, other)


if __name__ == "__main__":
    unittest.main()
