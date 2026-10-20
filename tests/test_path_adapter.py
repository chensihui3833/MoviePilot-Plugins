import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_adapter import adapter

with patch.dict(sys.modules, {"blurayepisodes": adapter}):
    path_adapter = importlib.import_module("blurayepisodes.path_adapter")


class PathAdapterTest(unittest.TestCase):
    def test_disc_parent_identity_scope_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            season = Path(directory) / "Show.S01"
            disc = season / "DISC1"
            (disc / "BDMV").mkdir(parents=True)
            calls = []

            class Planner:
                def _build_path_meta(self, path, custom_word_list=None,
                                     force_video=False, *, storage="local"):
                    calls.append((path, custom_word_list, storage))
                    return NS(type="TV", begin_season=1, end_season=None, name=path.name)

            original = Planner._build_path_meta
            plugin = NS(get_state=lambda: True)
            restore = path_adapter.install(plugin, Planner)
            with patch.dict(sys.modules, {"app.schemas.types": NS(MediaType=NS(TV="TV"))}):
                planner = Planner()
                self.assertEqual(planner._build_path_meta(disc, ["custom"]).name, "Show.S01")
                self.assertEqual(calls[-1], (season, ["custom"], "local"))
                self.assertEqual(planner._build_path_meta(disc, storage="remote").name, "DISC1")
                self.assertEqual(planner._build_path_meta(season / "Episode.mkv").name, "Episode.mkv")
                plugin.get_state = lambda: False
                self.assertEqual(planner._build_path_meta(disc).name, "DISC1")
            restore()
            self.assertIs(Planner._build_path_meta, original)

    def test_reload_does_not_stack_or_restore_over_new_adapter(self):
        class Planner:
            def _build_path_meta(self, *args, **kwargs):
                return None
        original = Planner._build_path_meta
        plugin = NS(get_state=lambda: False)
        old_restore = path_adapter.install(plugin, Planner)
        new_restore = path_adapter.install(plugin, Planner)
        old_restore()
        self.assertIs(Planner._build_path_meta._blurayepisodes_original, original)
        new_restore()
        self.assertIs(Planner._build_path_meta, original)
