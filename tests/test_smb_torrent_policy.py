import importlib.util
import sys
import unittest
from collections import UserDict
from pathlib import Path
from types import ModuleType, SimpleNamespace

PLUGIN = Path(__file__).parents[1] / "plugins.v3/smblinkclean"
package = ModuleType("smb_policy_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".torrent_policy", PLUGIN / "torrent_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
HASH = "a" * 40


class TorrentPolicyTests(unittest.TestCase):
    def test_mapping_based_qb_file_records_are_supported(self):
        task = policy.inventory(HASH, "smb:/源", [UserDict({"name": "pack/33.mkv"})])
        self.assertEqual(task["files"], ["/源/pack/33.mkv"])

    def test_qb_relative_file_joins_moviepilot_mapped_smb_save_path(self):
        files = [SimpleNamespace(name="剧名/S01E33.mkv"), SimpleNamespace(name="剧名/S01E34.mkv"),
                 SimpleNamespace(name="剧名/info.nfo")]
        task = policy.inventory(HASH, "smb:/源数据/电视剧/国产剧", files)
        self.assertEqual(task["files"], ["/源数据/电视剧/国产剧/剧名/S01E33.mkv", "/源数据/电视剧/国产剧/剧名/S01E34.mkv"])
        decision = policy.task_decision(task, [task["files"][0]])
        self.assertTrue(decision["must_pause"])
        self.assertFalse(decision["may_remove"])
        self.assertEqual(decision["matches"], [task["files"][0]])

    def test_multiepisode_removes_torrent_only_after_both_sources_retired(self):
        task = policy.inventory(HASH, "smb:/源/", [{"name": "pack/33.mkv"}, {"name": "pack/34.mkv"}])
        first, second = task["files"]
        self.assertFalse(policy.task_decision(task, [first], [first])["may_remove"])
        decision = policy.task_decision(task, [second], [first, second])
        self.assertTrue(decision["may_remove"])
        self.assertEqual(decision["remaining"], [])

    def test_single_video_plus_sidecar_is_one_video_but_not_before_retirement(self):
        task = policy.inventory(HASH, "smb:/源/电影", [{"name": "film.mkv"}, {"name": "info.nfo"}])
        path = task["files"][0]
        self.assertFalse(policy.task_decision(task, [path])["may_remove"])
        self.assertTrue(policy.task_decision(task, [path], [path])["may_remove"])

    def test_full_path_exact_match_not_basename_or_case_guess(self):
        task = policy.inventory(HASH, "smb:/源", [{"name": "show/E01.mkv"}])
        for candidate in ("/源/other/E01.mkv", "/源/show/e01.mkv", "/源x/show/E01.mkv"):
            self.assertEqual(policy.task_decision(task, [candidate])["matches"], [])

    def test_bad_and_unmapped_paths_are_not_trusted(self):
        cases = [("/volume1/video", "film.mkv"), ("smb:/源", "../film.mkv"),
                 ("smb:/源", "/absolute.mkv"), ("smb:/源/../库", "film.mkv"),
                 ("smb:/源", "evil\\film.mkv"), ("smb:/源", "smb:/other")]
        for root, name in cases:
            with self.subTest(root=root, name=name), self.assertRaises(ValueError):
                policy.remote_file(root, name)

    def test_unknown_empty_duplicate_or_bad_hash_is_rejected(self):
        for tid, files in (("x", [{"name": "x.mkv"}]), (HASH, []),
                           (HASH, [{"name": "x.nfo"}]),
                           (HASH, [{"name": "x.mkv"}, {"name": "x.mkv"}])):
            with self.subTest(tid=tid, files=files), self.assertRaises(ValueError):
                policy.inventory(tid, "smb:/源", files)


if __name__ == "__main__":
    unittest.main()
