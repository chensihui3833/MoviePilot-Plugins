import asyncio
import copy
import importlib
import re
import sys
import threading
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import test_core as fixtures

package = types.ModuleType("agent_test_package")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/blurayepisodes")]
sys.modules[package.__name__] = package
mapping = importlib.import_module("agent_test_package.agent_mapping")
bridge = importlib.import_module("agent_test_package.agent_bridge")


class AgentMappingTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CoreTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source, self.out = self.fixture.source, self.fixture.out
        self.result = {
            "season": 1, "unknown": [], "basis": "菜单按钮与逐盘播放列表关联",
            "evidence": ["DISC1/BDMV/JAR/00002/playlists.xml"],
            "episodes": [
                {"episode": 1, "disc": 1, "title": "First", "playlist": "DISC1/BDMV/PLAYLIST/00362.mpls", "video": "DISC1/BDMV/STREAM/00348.m2ts"},
                {"episode": 2, "disc": 1, "title": "Second", "playlist": "DISC1/BDMV/PLAYLIST/00335.mpls"},
                {"episode": 3, "disc": 2, "title": "Third", "playlist": "DISC2/BDMV/PLAYLIST/00334.mpls"},
            ],
        }
        self.probe = patch.object(mapping, "probe_video", return_value=(524280 / 45000, 60))
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def plan(self, value=None):
        return mapping.checked_plan(self.source, "Test Show", 1, self.out,
                                    value if value is not None else self.result, ffprobe="stub")

    def test_complete_mapping_keeps_semantic_order_not_clip_number(self):
        self.result["episodes"].reverse()
        plan = self.plan()
        self.assertEqual([r["clip"] for r in plan["episodes"]], ["00348", "00335", "00334"])
        self.assertEqual([r["disc_episode"] for r in plan["episodes"]], [1, 2, 1])
        self.assertFalse(self.out.exists())

    def test_unknown_wrong_season_duplicate_and_missing_disc_rejected(self):
        variants = []
        for field, value in (("unknown", ["unproved selection"]), ("season", 2), ("basis", "")):
            d = copy.deepcopy(self.result); d[field] = value; variants.append(d)
        d = copy.deepcopy(self.result); d["episodes"][1]["episode"] = 1; variants.append(d)
        d = copy.deepcopy(self.result); d["episodes"].pop(); variants.append(d)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(mapping.NeedsReview):
                self.plan(value)
        self.assertFalse(self.out.exists())

    def test_wrong_video_cross_disc_and_outside_evidence_rejected(self):
        for field, value in (("video", "DISC1/BDMV/STREAM/00335.m2ts"),
                             ("playlist", "DISC2/BDMV/PLAYLIST/00334.mpls")):
            d = copy.deepcopy(self.result); d["episodes"][0][field] = value
            with self.assertRaises(mapping.NeedsReview): self.plan(d)
        self.result["evidence"] = ["../outside.xml"]
        with self.assertRaises(mapping.NeedsReview): self.plan()

    def test_multi_item_partial_video_and_region_conflicts_rejected(self):
        target = self.source / self.result["episodes"][0]["playlist"]
        target.write_bytes(fixtures.mpls("00348", count=2))
        with self.assertRaises(mapping.NeedsReview): self.plan()
        target.write_bytes(fixtures.mpls("00348"))
        with patch.object(mapping, "probe_video", return_value=(524280 / 45000, 120)):
            with self.assertRaises(mapping.NeedsReview): self.plan()
        alt = target.with_name("00998.mpls"); alt.write_bytes(fixtures.mpls("00335"))
        self.result["episodes"][0]["alternatives"] = [str(alt.relative_to(self.source))]
        with self.assertRaises(mapping.NeedsReview): self.plan()

    def test_host_complete_season_count_catches_truncated_mapping(self):
        with self.assertRaises(mapping.NeedsReview):
            mapping.checked_plan(self.source, "Test Show", 1, self.out, self.result,
                                 ffprobe="stub", expected_episodes=[1, 2, 3, 4])

    def test_missing_video_is_failure(self):
        (self.source / "DISC1/BDMV/STREAM/00348.m2ts").unlink()
        with self.assertRaises((mapping.NeedsReview, OSError)): self.plan()

    def test_snapshot_tracks_metadata_and_video_identity(self):
        before = mapping.snapshot(self.source)
        p = self.source / "DISC1/BDMV/JAR/00002/playlists.xml"
        p.write_bytes(p.read_bytes() + b"\n")
        self.assertNotEqual(mapping.snapshot(self.source), before)
        before = mapping.snapshot(self.source)
        p = self.source / "DISC1/BDMV/STREAM/00348.m2ts"
        p.write_bytes(p.read_bytes() + b"changed")
        self.assertNotEqual(mapping.snapshot(self.source), before)

    def test_cache_reused_then_source_change_reanalyzes(self):
        store = {}
        plugin = types.SimpleNamespace(_config={}, get_data=store.get,
                                       save_data=lambda k, v: store.update({k: v}))
        with patch.object(bridge, "analyze", return_value=self.result) as analyze, \
             patch.object(mapping.shutil, "which", return_value="stub"):
            for _ in range(2):
                mapping.scan_with_agent(plugin, self.source, "Test Show", 1, self.out)
            self.assertEqual(analyze.call_count, 1)
            p = self.source / "DISC1/BDMV/JAR/00002/playlists.xml"
            p.write_bytes(p.read_bytes() + b"\n")
            mapping.scan_with_agent(plugin, self.source, "Test Show", 1, self.out)
            self.assertEqual(analyze.call_count, 2)
        self.assertFalse(self.out.exists())

    def test_source_change_during_analysis_and_unknown_not_cached(self):
        store = {}
        plugin = types.SimpleNamespace(_config={}, get_data=store.get,
                                       save_data=lambda k, v: store.update({k: v}))
        def changed(*args):
            p = self.source / "DISC1/BDMV/JAR/00002/playlists.xml"
            p.write_bytes(p.read_bytes() + b"\n")
            return self.result
        with patch.object(bridge, "analyze", side_effect=changed), \
             patch.object(mapping.shutil, "which", return_value="stub"):
            with self.assertRaises(mapping.NeedsReview):
                mapping.scan_with_agent(plugin, self.source, "Test Show", 1, self.out)
        self.assertEqual(store, {})

    def test_preview_only_blocks_real_transfer_before_analysis(self):
        from test_native import NativeTest, native
        f = NativeTest(); f.setUp(); self.addCleanup(f.tearDown)
        f.plugin._config = {}
        with patch.dict(sys.modules, f.host), patch.object(native, "scan_with_agent") as analyze:
            result = native.native_transfer(f.plugin, f.file, f.meta, f.media, target_path=f.out)
        self.assertFalse(result.success)
        self.assertIn("仅预览", result.message)
        analyze.assert_not_called()
        self.assertFalse(f.out.exists())

    def test_native_real_pipeline_preview_hardlinks_cache_and_no_disc_layer(self):
        from test_native import NativeTest, native, native_core
        f = NativeTest(); f.setUp(); self.addCleanup(f.tearDown)
        plugin = f.plugin
        plugin._config = {"preview_only": False}; plugin.get_data = plugin.saved.get
        before = f.source_hashes()
        # Use this fixture's equivalent source path; same nontrivial clip/playlist identities.
        with patch.dict(sys.modules, f.host), \
             patch.object(native, "scan_with_agent", side_effect=mapping.scan_with_agent), \
             patch.object(bridge, "analyze", return_value=self.result) as analyze, \
             patch.object(mapping.shutil, "which", return_value="stub"), \
             patch.object(native_core, "scan_season", side_effect=AssertionError("legacy recognizer must not run")):
            preview = native.native_transfer(plugin, f.file, f.meta, f.media, target_path=f.out, preview=True)
            self.assertTrue(preview.success, preview.message)
            self.assertFalse(f.out.exists())
            for disc in (1, 2):
                f.file.path = str(f.source / f"DISC{disc}")
                result = native.native_transfer(plugin, f.file, f.meta, f.media, target_path=f.out)
                self.assertTrue(result.success, result.message)
                for src, dst in zip(result.file_list, result.file_list_new):
                    self.assertTrue(Path(src).samefile(dst))
                    self.assertEqual(Path(dst).parent, (f.out / "Test Show/Season 1").resolve())
            self.assertEqual(analyze.call_count, 1)
        self.assertEqual(len(list(f.out.rglob("*.m2ts"))), 3)
        self.assertEqual(f.source_hashes(), before)


class BridgeTest(unittest.TestCase):
    def test_submission_bound_to_session_and_expired_job(self):
        job, prefix = bridge.broker.open()
        self.addCleanup(bridge.broker.close, job)
        with self.assertRaises(bridge.NeedsReview): bridge.broker.submit(job, "other", {})
        bridge.broker.submit(job, prefix + "_abc__", {"season": 1})
        self.assertEqual(bridge.broker.take(job), {"season": 1})
        with self.assertRaises(bridge.NeedsReview): bridge.broker.submit(job, prefix + "_abc__", {})
        bridge.broker.close(job)
        with self.assertRaises(bridge.NeedsReview): bridge.broker.submit(job, prefix + "_abc__", {})

    def test_stop_and_timeout_cancel_future(self):
        f = Future()
        with self.assertRaises(bridge.NeedsReview): bridge.wait_for_analysis(f, 10, lambda: True)
        self.assertTrue(f.cancelled())
        f = Future()
        with patch.object(bridge.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(bridge.NeedsReview): bridge.wait_for_analysis(f, 1)
        self.assertTrue(f.cancelled())

    def test_real_event_loop_background_bridge_and_cleanup(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever); thread.start()
        calls = []
        async def run(**kw):
            calls.append(kw)
            job = re.search(r"job_id=([0-9a-f]+)", kw["message"])[1]
            bridge.broker.submit(job, kw["session_prefix"] + "_abc__", {"season": 1})
        modules = {"app.application.agent": types.SimpleNamespace(
            get_running_agent_manager=lambda: types.SimpleNamespace(run_background_prompt=run)),
            "app.runtime.loop": types.SimpleNamespace(main_loop_registry=types.SimpleNamespace(require=lambda: loop))}
        try:
            with patch.dict(sys.modules, modules):
                result = bridge.analyze(Path("/test/source"), 1, 30)
            self.assertEqual(result, {"season": 1})
            self.assertFalse(calls[0]["allow_message_tools"])
            job = re.search(r"job_id=([0-9a-f]+)", calls[0]["message"])[1]
            self.assertIsNone(bridge.broker.take(job))
        finally:
            loop.call_soon_threadsafe(loop.stop); thread.join(); loop.close()


if __name__ == "__main__":
    unittest.main()
