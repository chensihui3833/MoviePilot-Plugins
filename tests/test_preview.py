import importlib
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_adapter import adapter

with patch.dict(sys.modules, {"blurayepisodes": adapter}):
    preview = importlib.import_module("blurayepisodes.preview_adapter")


class PreviewTest(unittest.TestCase):
    def test_three_discs_show_ten_episodes_with_four_four_two(self):
        items, plans = [], {}
        number = 0
        for disc, count in enumerate([4, 4, 2], 1):
            source = f"/source/DISC{disc}"
            rows = []
            for _ in range(count):
                number += 1
                rows.append(dict(disc=disc, episode=number, relative=f"E{number}.m2ts",
                                 title=f"Title {number}"))
            plans[source] = dict(output="/output", season=1, episodes=rows)
            items.append(dict(source=source, source_item={"name": f"DISC{disc}", "path": source},
                              target=str(Path("/output")/rows[0]["relative"]), success=True))
        before = deepcopy(items)
        result = preview.expand_items(items, plans)
        self.assertEqual([r["episode"] for r in result], list(range(1, 11)))
        self.assertEqual(len({r["target"] for r in result}), 10)
        self.assertIn("DISC1（4 集，E01–E04）", result[0]["source_item"]["name"])
        self.assertIn("DISC2（4 集，E05–E08）", result[4]["message"])
        self.assertIn("DISC3（2 集，E09–E10）", result[8]["message"])
        self.assertEqual(items, before)
        self.assertEqual(result[0]["source"], items[0]["source"])
        failed = dict(items[0], success=False)
        unrelated = dict(items[0], target="/other/file.m2ts")
        self.assertEqual(preview.expand_items([failed, unrelated], plans), [failed, unrelated])

    def test_actual_execution_and_disabled_preview_are_unchanged(self):
        class Collector:
            def result(self, success, errors, *, preview, preview_items):
                return preview_items
        original = Collector.result
        plugin = NS(get_state=lambda: True, _preview_plans={})
        restore = preview.install(plugin, Collector)
        items = [{"source": "/ordinary.mkv"}]
        with patch.object(preview, "expand_items") as expand:
            self.assertIs(Collector().result(True, [], preview=False, preview_items=items), items)
            plugin.get_state = lambda: False
            self.assertIs(Collector().result(True, [], preview=True, preview_items=items), items)
            expand.assert_not_called()
        restore()
        self.assertIs(Collector.result, original)
