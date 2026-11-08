import importlib.util
import importlib
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

PLUGIN = Path(__file__).parents[1] / "plugins.v3/smblinkclean"
package = ModuleType("smb_qb_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".qb_bridge", PLUGIN / "qb_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
H1, H2 = "a" * 40, "b" * 40


class Client:
    def __init__(self):
        self.rows = {H1: {"hash": H1, "save_path": "/volume1/video/源", "state": "uploading", "progress": 1.0}}
        self.files = {H1: [{"name": "pack/33.mkv"}, {"name": "pack/34.mkv"}, {"name": "pack/info.nfo"}]}
        self.error, self.pause_ack, self.pause_changes, self.deletes = False, True, True, []
    def get_torrents(self, ids=None):
        return ([dict(row) for key, row in self.rows.items() if not ids or key == ids], self.error)
    def get_files(self, tid): return self.files.get(tid)
    def stop_torrents(self, ids):
        if self.pause_changes: self.rows[ids]["state"] = "stoppedUP"
        return self.pause_ack
    def delete_torrents(self, ids, delete_file):
        self.deletes.append((ids, delete_file)); self.rows.pop(ids, None); return True


class Module:
    def __init__(self, client): self.client = client
    def get_instance(self, name): return self.client if name == "群晖 QB" else None
    def normalize_return_path(self, path, name):
        text = str(path)
        return "smb:" + text.removeprefix("/volume1/video") if text.startswith("/volume1/video/") else text


class QBBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.qb = bridge.QBBridge(Module(self.client), "群晖 QB")
        self.candidate = "/源/pack/33.mkv"

    def test_query_failure_is_not_treated_as_no_tasks(self):
        self.client.error = True
        with self.assertRaises(bridge.QBUnavailable): self.qb.tasks_for([self.candidate])
        self.client.error = False
        self.client.rows = {}
        self.assertEqual(self.qb.tasks_for([self.candidate]), [])

    def test_finds_multiple_tasks_that_reference_exact_same_source(self):
        self.client.rows[H2] = {**self.client.rows[H1], "hash": H2}
        self.client.files[H2] = [{"name": "pack/33.mkv"}]
        tasks = self.qb.tasks_for([self.candidate])
        self.assertEqual({task["hash"] for task in tasks}, {H1, H2})

    def test_pause_is_verified_and_deletion_never_requests_file_removal(self):
        task = self.qb.tasks_for([self.candidate])[0]
        self.assertTrue(self.qb.pause(task))
        self.assertTrue(self.qb.remove(task))
        self.assertEqual(self.client.deletes, [(H1, False)])

    def test_success_ack_without_actual_pause_does_not_authorize_removal(self):
        task = self.qb.tasks_for([self.candidate])[0]
        self.client.pause_changes = False
        with patch.object(bridge.time, "sleep", lambda _: None):
            self.assertFalse(self.qb.pause(task))
        self.assertFalse(self.qb.remove(task))
        self.assertEqual(self.client.deletes, [])

    def test_manifest_or_save_path_changes_prevent_task_removal(self):
        task = self.qb.tasks_for([self.candidate])[0]
        self.assertTrue(self.qb.pause(task))
        self.client.files[H1].append({"name": "pack/35.mkv"})
        self.assertFalse(self.qb.remove(task))
        self.assertEqual(self.client.deletes, [])

    def test_missing_file_list_or_unfinished_torrent_blocks_work(self):
        self.client.files[H1] = None
        with self.assertRaises(bridge.QBUnavailable): self.qb.tasks_for([self.candidate])
        self.client.files[H1] = [{"name": "pack/33.mkv"}]
        for progress, state in ((0.5, "uploading"), (float('nan'), "uploading"), (1, "checkingUP")):
            self.client.rows[H1].update(progress=progress, state=state)
            with self.subTest(progress=progress, state=state), self.assertRaises(bridge.QBUnavailable):
                self.qb.tasks_for([self.candidate])

    def test_unknown_downloader_rejected(self):
        with self.assertRaises(bridge.QBUnavailable): bridge.QBBridge(Module(self.client), "other")

    def test_only_known_paused_partial_pack_can_be_processed_after_first_retirement(self):
        task = self.qb.tasks_for([self.candidate])[0]
        self.client.rows[H1].update(state="stoppedDL", progress=0.5)
        with self.assertRaises(bridge.QBUnavailable): self.qb.tasks_for([self.candidate])
        history = {H1: {"files": task["files"], "retired": [task["files"][1]], "removed": False}}
        self.assertEqual(len(self.qb.tasks_for([self.candidate], history)), 1)
        self.client.rows[H1]["state"] = "downloading"
        with self.assertRaises(bridge.QBUnavailable): self.qb.tasks_for([self.candidate], history)
        self.client.rows[H1]["state"] = "stoppedDL"
        self.client.files[H1].append({"name": "pack/35.mkv"})
        with self.assertRaises(bridge.QBUnavailable): self.qb.tasks_for([self.candidate], history)

    def test_individual_task_lookup_distinguishes_absent_and_disconnected(self):
        task = self.qb.get_task(H1)
        self.assertEqual(task["hash"], H1)
        self.client.rows.pop(H1)
        self.assertIsNone(self.qb.get_task(H1))
        self.client.error = True
        with self.assertRaises(bridge.QBUnavailable): self.qb.get_task(H1)


class QBFleetTests(unittest.TestCase):
    def setUp(self):
        self.first, self.second = Client(), Client()
        self.fleet = bridge.QBFleet({"本地 QB": bridge.QBBridge(Module(self.first), "群晖 QB"),
                                    "群晖 QB": bridge.QBBridge(Module(self.second), "群晖 QB")})
        self.path = "/源/pack/33.mkv"

    def test_same_hash_in_both_downloaders_is_two_distinct_tasks(self):
        tasks = self.fleet.tasks_for([self.path])
        self.assertEqual(len(tasks), 2)
        self.assertNotEqual(tasks[0]["hash"], tasks[1]["hash"])
        for task in tasks:
            self.assertTrue(self.fleet.pause(task))
            self.assertTrue(self.fleet.verify_task(task))
            self.assertTrue(self.fleet.remove(task))
        self.assertEqual(self.first.deletes, [(H1, False)])
        self.assertEqual(self.second.deletes, [(H1, False)])

    def test_one_unavailable_downloader_blocks_before_any_pause(self):
        self.second.error = True
        with self.assertRaises(bridge.QBUnavailable): self.fleet.tasks_for([self.path])
        self.assertEqual(self.first.rows[H1]["state"], "uploading")
        self.assertEqual(self.first.deletes, [])

    def test_unknown_or_malformed_namespaced_task_is_rejected(self):
        for key in ('bad', '["不存在","' + H1 + '"]', '["本地 QB",null]'):
            with self.subTest(key=key), self.assertRaises(bridge.QBUnavailable):
                self.fleet.get_task(key)

    def test_known_partial_state_is_kept_separate_for_each_downloader(self):
        tasks = self.fleet.tasks_for([self.path])
        self.first.rows[H1].update(state="stoppedDL", progress=0.5)
        with self.assertRaises(bridge.QBUnavailable): self.fleet.tasks_for([self.path])
        history = {tasks[0]["hash"]: {"files": tasks[0]["files"], "retired": [tasks[0]["files"][1]]}}
        self.assertEqual(len(self.fleet.tasks_for([self.path], history)), 2)

    def test_cross_downloader_seeders_both_pause_before_source_cleanup(self):
        coordinator = importlib.import_module(package.__name__ + ".torrent_coordinator")
        journal, deleted = {}, []
        def delete(check):
            self.assertEqual(self.first.rows[H1]["state"], "stoppedUP")
            self.assertEqual(self.second.rows[H1]["state"], "stoppedUP")
            if not check(): return False
            deleted.append(self.path)
            return True
        candidate = {"source": self.path, "identity": {"ino": 7}}
        outcome = coordinator.run_candidate(self.fleet, candidate, journal, lambda _: None, delete, lambda _: True)
        self.assertTrue(outcome["cleaned"])
        self.assertEqual(deleted, [self.path])
        self.assertEqual(outcome["removed_tasks"], 0)
        self.assertEqual(len(journal["tasks"]), 2)


if __name__ == "__main__": unittest.main()
