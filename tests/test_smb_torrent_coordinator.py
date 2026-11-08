import importlib.util
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType

PLUGIN = Path(__file__).parents[1] / "plugins.v3/smblinkclean"
package = ModuleType("smb_coordinator_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".torrent_coordinator", PLUGIN / "torrent_coordinator.py")
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)
A, B, C = "/源/33.mkv", "/源/34.mkv", "/源/35.mkv"


def candidate(path): return {"source": path, "identity": {"ino": 1, "path": path}}


class FakeQB:
    def __init__(self, tasks):
        self.tasks = deepcopy(tasks)
        self.paused, self.removed, self.events = set(), set(), []
        self.pause_fail, self.verify_ok, self.remove_ok = False, True, True
    def tasks_for(self, candidates, known_retired=None):
        self.events.append("query")
        return [deepcopy(task) for task in self.tasks if task["hash"] not in self.removed
                and set(task["files"]) & set(candidates)]
    def pause(self, task):
        self.events.append("pause:" + task["hash"])
        if self.pause_fail: return False
        self.paused.add(task["hash"]); return True
    def verify_task(self, task): return self.verify_ok and task["hash"] in self.paused
    def remove(self, task):
        self.events.append("remove:" + task["hash"])
        if not self.remove_ok: return False
        self.removed.add(task["hash"]); return True
    def get_task(self, tid):
        if tid in self.removed: return None
        return next((deepcopy(task) for task in self.tasks if task["hash"] == tid), None)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.journal, self.saved, self.deleted = {}, [], []
    def save(self, state): self.saved.append(deepcopy(state))
    def delete(self, check):
        if not check(): return False
        self.deleted.append(True); return True

    def test_single_video_pauses_then_persists_intent_then_cleans_then_removes_task(self):
        qb = FakeQB([{"hash": "a", "files": [A]}])
        result = coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        self.assertTrue(result["cleaned"])
        self.assertEqual(result["removed_tasks"], 1)
        self.assertEqual(self.saved[0]["intents"][A]["state"], "pending")
        self.assertEqual(self.saved[1]["intents"][A]["state"], "removed")
        self.assertIn("a", qb.removed)

    def test_multiepisode_stays_paused_until_all_videos_cleaned(self):
        qb = FakeQB([{"hash": "a", "files": [A, B]}])
        first = coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        self.assertEqual(first["removed_tasks"], 0)
        self.assertIn("a", qb.paused)
        second = coordinator.run_candidate(qb, candidate(B), self.journal, self.save, self.delete, lambda _: True)
        self.assertEqual(second["removed_tasks"], 1)
        self.assertEqual(set(self.journal["tasks"]["a"]["retired"]), {A, B})

    def test_no_torrent_or_failed_pause_never_cleans(self):
        qb = FakeQB([])
        self.assertFalse(coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)["cleaned"])
        qb = FakeQB([{"hash": "a", "files": [A]}]); qb.pause_fail = True
        with self.assertRaises(coordinator.CoordinationError):
            coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        self.assertEqual(self.deleted, [])

    def test_all_cross_seed_tasks_paused_before_deleting_source(self):
        qb = FakeQB([{"hash": "a", "files": [A]}, {"hash": "b", "files": [A, B]}])
        def delete(check):
            self.assertEqual(qb.paused, {"a", "b"})
            return check()
        result = coordinator.run_candidate(qb, candidate(A), self.journal, self.save, delete, lambda _: True)
        self.assertEqual(result["removed_tasks"], 1)
        self.assertEqual(qb.removed, {"a"})
        self.assertEqual(self.journal["tasks"]["b"]["retired"], [A])

    def test_changed_qb_tasks_at_deletion_time_abort(self):
        qb = FakeQB([{"hash": "a", "files": [A]}])
        def delete(check):
            qb.tasks.append({"hash": "new", "files": [A]})
            return check()
        result = coordinator.run_candidate(qb, candidate(A), self.journal, self.save, delete, lambda _: True)
        self.assertFalse(result["cleaned"])
        self.assertEqual(qb.removed, set())

    def test_persistence_failures_never_delete_a_torrent(self):
        for fail_on in (1, 2):
            qb = FakeQB([{"hash": "a", "files": [A]}]); calls = []
            def save(state):
                calls.append(state)
                if len(calls) == fail_on: raise OSError("database unavailable")
            with self.subTest(fail_on=fail_on), self.assertRaises(OSError):
                coordinator.run_candidate(qb, candidate(A), {}, save, lambda check: check(), lambda _: True)
            self.assertEqual(qb.removed, set())

    def test_changed_manifest_and_prior_removed_intent_fail_closed(self):
        qb = FakeQB([{"hash": "a", "files": [A, B]}])
        self.journal["tasks"] = {"a": {"files": [A, C], "retired": [], "removed": False}}
        with self.assertRaises(coordinator.CoordinationError):
            coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        self.assertEqual(self.deleted, [])

    def test_retry_removal_only_after_every_retired_source_is_still_absent(self):
        qb = FakeQB([{"hash": "a", "files": [A]}]); qb.remove_ok = False
        coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        self.assertFalse(self.journal["tasks"]["a"]["removed"])
        qb.remove_ok = True
        self.assertEqual(coordinator.finalize_ready(qb, self.journal, self.save, lambda _: False), 0)
        self.assertEqual(qb.removed, set())
        self.assertEqual(coordinator.finalize_ready(qb, self.journal, self.save, lambda _: True), 1)
        self.assertEqual(qb.removed, {"a"})

    def test_retry_does_not_remove_changed_manifest(self):
        qb = FakeQB([{"hash": "a", "files": [A]}]); qb.remove_ok = False
        coordinator.run_candidate(qb, candidate(A), self.journal, self.save, self.delete, lambda _: True)
        qb.tasks[0]["files"].append(B)
        with self.assertRaises(coordinator.CoordinationError):
            coordinator.finalize_ready(qb, self.journal, self.save, lambda _: True)
        self.assertEqual(qb.removed, set())


if __name__ == "__main__": unittest.main()
