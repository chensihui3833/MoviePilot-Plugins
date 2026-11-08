import sys
import threading
import types
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from test_smb_link_clean import load_plugin, item


class Storage:
    def _check_connection(self): pass
    def _normalize_path(self, path, writable=False): return "\\\\server\\share" + path.replace("/", "\\")


class FakeQB:
    def __init__(self, files):
        self.files = files
        self.paused = False
        self.removed = False
    def tasks_for(self, paths, known_retired=None):
        return [] if self.removed or not set(paths) & set(self.files) else [{"hash": "a" * 40, "files": self.files}]
    def pause(self, task): self.paused = True; return True
    def verify_task(self, task): return self.paused and not self.removed
    def remove(self, task):
        if not self.verify_task(task): return False
        self.removed = True
        return True
    def get_task(self, tid): return None if self.removed else {"hash": tid, "files": self.files}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = load_plugin()
        fake_storage = types.ModuleType("app.modules.filemanager.storages.smb")
        fake_storage.SMB = Storage
        self.modules = {name: types.ModuleType(name) for name in
                        ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages", "smbclient")}
        self.modules["app.modules.filemanager.storages.smb"] = fake_storage
        self.sources = {"/源/33.mkv": item(33, "/源/33.mkv"), "/源/34.mkv": item(34, "/源/34.mkv")}
        self.library = {"/库/33.mkv": item(33, "/库/33.mkv"), "/库/34.mkv": item(34, "/库/34.mkv")}
        self.qb = FakeQB(["/源/33.mkv", "/源/34.mkv"])
        self.saved = {}
        self.instance = self.plugin.SmbLinkClean()
        self.instance._config = {"source": "/源", "library": "/库", "enabled": True,
                                 "auto_delete": True, "qb_name": "群晖 QB"}
        self.instance.get_data = lambda key: deepcopy(self.saved.get(key))
        self.instance.save_data = lambda key, value: self.saved.__setitem__(key, deepcopy(value))

    def scan_tree(self, client, unc, logical, max_files=50000):
        if logical == "/源": return deepcopy(self.sources)
        if logical == "/库": return deepcopy(self.library)
        raise OSError("unexpected directory")

    def delete_verified(self, path, expected, recheck):
        if not recheck(): return False
        return self.sources.pop(expected["path"].casefold(), None) is not None

    def test_two_episode_washes_pause_then_remove_task_without_touching_other_episode(self):
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: self.qb):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.library["/库/33.mkv"] = item(133, "/库/33.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1001): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): self.instance.scan()
            self.assertNotIn("/源/33.mkv", self.sources)
            self.assertIn("/源/34.mkv", self.sources)
            self.assertTrue(self.qb.paused)
            self.assertFalse(self.qb.removed)
            self.assertEqual(self.saved["status"]["removed"], 1)
            self.library["/库/34.mkv"] = item(134, "/库/34.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1602): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=2202): self.instance.scan()
            self.assertNotIn("/源/34.mkv", self.sources)
            self.assertTrue(self.qb.removed)
            self.assertEqual(self.saved["status"]["removed_tasks"], 1)

    def test_disabling_waits_for_in_flight_cleanup_before_applying(self):
        entered, release, disabled = threading.Event(), threading.Event(), threading.Event()
        def slow_delete(path, expected, recheck):
            entered.set()
            if not release.wait(2): raise TimeoutError("test cleanup did not resume")
            return self.delete_verified(path, expected, recheck)
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", slow_delete), \
                patch.object(self.plugin, "existing_qb", lambda name: self.qb):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.library["/库/33.mkv"] = item(133, "/库/33.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1001): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601):
                scan = threading.Thread(target=self.instance.scan)
                scan.start()
                self.assertTrue(entered.wait(1))
                def disable():
                    self.instance.init_plugin({"enabled": False, "auto_delete": False})
                    disabled.set()
                change = threading.Thread(target=disable)
                change.start()
                self.assertFalse(disabled.wait(0.05))
                release.set()
                scan.join(2); change.join(2)
                self.assertTrue(disabled.is_set())
                self.assertFalse(self.instance.get_state())

    def test_restart_between_episodes_recovers_paused_pack(self):
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: self.qb):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.library["/库/33.mkv"] = item(133, "/库/33.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1001): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): self.instance.scan()
            self.assertNotIn("/源/33.mkv", self.sources)
            self.assertTrue(self.qb.paused)
            self.assertFalse(self.qb.removed)
            restarted = self.plugin.SmbLinkClean()
            restarted._config = deepcopy(self.instance._config)
            restarted._force_baseline = True
            restarted.get_data = lambda key: deepcopy(self.saved.get(key))
            restarted.save_data = lambda key, value: self.saved.__setitem__(key, deepcopy(value))
            with patch.object(self.plugin.time, "monotonic", return_value=1700): restarted.scan()
            self.library["/库/34.mkv"] = item(134, "/库/34.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1701): restarted.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=2301): restarted.scan()
            self.assertNotIn("/源/34.mkv", self.sources)
            self.assertTrue(self.qb.removed)
            self.assertEqual(self.saved["status"]["removed_tasks"], 1)

    def test_unresolved_intent_blocks_other_automatic_actions_after_restart(self):
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: self.qb):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            scope = self.saved["snapshot"]["scope"]
            import hashlib
            journal_scope = hashlib.sha256((scope + "\0群晖 QB").encode()).hexdigest()
            self.saved["qb_journal"] = {"scope": journal_scope,
                "tasks": {"a" * 40: {"files": ["/源/33.mkv"], "retired": [], "removed": False}},
                "intents": {"/源/33.mkv": {"state": "pending"}}}
            self.instance._force_baseline = True
            with patch.object(self.plugin.time, "monotonic", return_value=1100): self.instance.scan()
            self.assertFalse(self.saved["status"]["ok"])
            self.assertEqual(self.saved["status"]["error"], "CoordinationError")
            self.assertEqual(len(self.sources), 2)
            self.assertFalse(self.qb.paused)
            self.assertFalse(self.qb.removed)

    def test_two_downloaders_same_torrent_both_pause_and_finish(self):
        other = FakeQB(self.qb.files)
        self.instance._config["qb_names"] = ["群晖 QB", "本地 QB"]
        clients = {"群晖 QB": self.qb, "本地 QB": other}
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: clients[name]):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.library["/库/33.mkv"] = item(133, "/库/33.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1001): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): self.instance.scan()
            self.assertNotIn("/源/33.mkv", self.sources)
            self.assertIn("/源/34.mkv", self.sources)
            self.assertTrue(self.qb.paused and other.paused)
            self.assertFalse(self.qb.removed or other.removed)
            self.library["/库/34.mkv"] = item(134, "/库/34.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1602): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=2202): self.instance.scan()
            self.assertTrue(self.qb.removed and other.removed)
            self.assertEqual(self.saved["status"]["removed_tasks"], 2)

    def test_changing_downloaders_with_unfinished_journal_does_not_clear_it(self):
        other = FakeQB(self.qb.files)
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: {"群晖 QB": self.qb, "本地 QB": other}[name]):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.library["/库/33.mkv"] = item(133, "/库/33.mkv")
            with patch.object(self.plugin.time, "monotonic", return_value=1001): self.instance.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): self.instance.scan()
            before = deepcopy(self.saved["qb_journal"])
            self.instance._config["qb_names"] = ["本地 QB", "群晖 QB"]
            with patch.object(self.plugin.time, "monotonic", return_value=1602): self.instance.scan()
            self.assertFalse(self.saved["status"]["ok"])
            self.assertEqual(self.saved["status"]["error"], "CoordinationError")
            self.assertEqual(self.saved["qb_journal"], before)
            self.assertIn("/源/34.mkv", self.sources)

    def test_explicitly_empty_selection_blocks_cleanup_even_with_legacy_name(self):
        self.instance._config["qb_names"] = []
        with patch.dict(sys.modules, self.modules), patch.object(self.plugin, "scan_tree", self.scan_tree), \
                patch.object(self.plugin, "delete_verified", self.delete_verified), \
                patch.object(self.plugin, "existing_qb", lambda name: self.qb):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): self.instance.scan()
            self.assertFalse(self.saved["status"]["ok"])
            self.assertEqual(self.saved["status"]["detail"], "请选择至少一个 QB 下载器")
            self.assertFalse(self.qb.paused)
            self.assertEqual(len(self.sources), 2)


if __name__ == "__main__": unittest.main()
