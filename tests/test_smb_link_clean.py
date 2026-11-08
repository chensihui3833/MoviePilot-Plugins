import importlib.util
import stat
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins.v3/smblinkclean"


def load_plugin():
    modules = {name: types.ModuleType(name) for name in ("app", "app.runtime", "app.runtime.log", "app.sdk", "app.sdk.plugin", "app.sdk.plugin.base")}
    modules["app.runtime.log"].logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    modules["app.sdk.plugin.base"]._PluginBase = object
    spec = importlib.util.spec_from_file_location("smblinkclean_test", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    modules["smblinkclean_test"] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        core = sys.modules["smblinkclean_test.core"]
    return module, core


def item(ino, name="/源/Video.MKV", size=100, mtime=1000, dev=1):
    return {"dev": dev, "ino": ino, "size": size, "mtime_ns": mtime, "ctime_ns": 500, "path": name}


def snapshot(sources=None, library=None, scope="share", candidates=None):
    return {"scope": scope, "sources": sources or {}, "library": library or {}, "candidates": candidates or []}


class SmbLinkCleanTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.core = load_plugin()
        self.source = item(10)
        self.library = item(10, "/库/Video.MKV")
        self.before = snapshot({"/源/video.mkv": self.source}, {"/库/video.mkv": self.library})

    def test_initial_and_scope_changes_only_baseline(self):
        now = snapshot({"/源/video.mkv": self.source}, {})
        self.assertEqual(self.core.calculate(None, now), [])
        now["scope"] = "different share"
        self.assertEqual(self.core.calculate(self.before, now), [])

    def test_deleted_and_replaced_library_find_source(self):
        deleted = snapshot({"/源/video.mkv": self.source}, {})
        result = self.core.calculate(self.before, deleted)
        self.assertEqual([(x["source"], x["library"], x["reason"]) for x in result],
                         [("/源/Video.MKV", "/库/Video.MKV", "删除")])
        replacement = snapshot({"/源/video.mkv": self.source}, {"/库/video.mkv": item(20, "/库/Video.MKV")})
        self.assertEqual(self.core.calculate(self.before, replacement)[0]["reason"], "替换")

    def test_rename_or_other_library_link_does_not_trigger(self):
        now = snapshot({"/源/video.mkv": self.source}, {"/库/renamed.mkv": item(10, "/库/Renamed.MKV")})
        self.assertEqual(self.core.calculate(self.before, now), [])

    def test_source_change_and_identity_change_are_not_candidates(self):
        for changed in (item(11), item(10, size=101), item(10, mtime=1001), item(10, dev=2), {**item(10), "ctime_ns": 501}):
            self.assertEqual(self.core.calculate(self.before, snapshot({"/源/video.mkv": changed}, {})), [])

    def test_pending_is_retained_then_removed_if_source_changes_or_library_returns(self):
        now = snapshot({"/源/video.mkv": self.source}, {})
        now["candidates"] = self.core.calculate(self.before, now)
        self.assertEqual(len(self.core.calculate(now, snapshot({"/源/video.mkv": self.source}, {}))), 1)
        self.assertEqual(self.core.calculate(now, snapshot({"/源/video.mkv": item(11)}, {})), [])
        self.assertEqual(self.core.calculate(now, snapshot({"/源/video.mkv": self.source}, {"/库/reappeared": item(10)})), [])

    def test_path_guards(self):
        for source, library in (("/", "/lib"), ("../x", "/lib"), ("/a/../b", "/lib"),
                                ("/a", "/a/b"), ("/a", "/A"), ("//host/share", "/lib")):
            with self.subTest(source=source, library=library), self.assertRaises(ValueError):
                self.core.validate_roots(source, library)
        self.assertEqual(self.core.validate_roots("/源/", "/库/"), ("/源", "/库"))

    def test_multiple_pairs_cover_movies_and_series_without_overlap(self):
        self.assertEqual(self.core.parse_pairs("/源/电影|/电影\n/源/电视剧|/电视剧"),
                         [("/源/电影", "/电影"), ("/源/电视剧", "/电视剧")])
        for value in ("/源/电影|/电影\n/源|/电视剧", "/源|/库\n/其他|/库", "/源, /库", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.core.parse_pairs(value)

    def test_unusable_id_or_metadata_fails(self):
        for values in ((1, 0, 4, 1), (None, 1, 4, 1), (1, 2, None, 1), (1, 2, 4, None)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.core.identity(SimpleNamespace(st_dev=values[0], st_ino=values[1], st_size=values[2], st_mtime_ns=values[3], st_ctime_ns=5))

    def test_scan_incomplete_or_outside_scope_fails(self):
        class Client:
            def walk(self, root, **kwargs):
                kwargs["onerror"](OSError("disconnected"))
                yield
        with self.assertRaises(OSError):
            self.plugin.scan_tree(Client(), "\\\\server\\share\\library", "/library")
        class Escape:
            def walk(self, root, **kwargs):
                yield "\\\\server\\other", [], ["x.mkv"]
        with self.assertRaises(ValueError):
            self.plugin.scan_tree(Escape(), "\\\\server\\share\\library", "/library")

    def test_scan_collects_media_and_skips_symlinks(self):
        class Client:
            def walk(self, root, **kwargs):
                yield root, [], ["Film.MKV", "poster.jpg", "Link.mkv"]
            def lstat(self, path):
                mode = stat.S_IFLNK if path.endswith("Link.mkv") else stat.S_IFREG
                return SimpleNamespace(st_mode=mode, st_dev=1, st_ino=7, st_size=8, st_mtime_ns=9, st_ctime_ns=5)
        result = self.plugin.scan_tree(Client(), "\\\\server\\share\\library", "/library")
        self.assertEqual(list(result), ["/library/film.mkv"])
        self.assertEqual(result["/library/film.mkv"]["path"], "/library/Film.MKV")

    def test_complete_scans_and_disconnect_preserve_previous_snapshot(self):
        class Storage:
            def _check_connection(self):
                pass
            def _normalize_path(self, path, writable=False):
                return "\\\\server\\share" + path.replace("/", "\\")

        class Client(types.ModuleType):
            fail = False
            library_ino = 10
            def walk(self, root, **kwargs):
                if self.fail:
                    kwargs["onerror"](OSError("connection lost"))
                yield root, [], ["Video.MKV"]
            def lstat(self, path):
                ino = self.library_ino if "\\库\\" in path else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)

        client = Client("smbclient")
        fake_storage = types.ModuleType("app.modules.filemanager.storages.smb")
        fake_storage.SMB = Storage
        modules = {name: types.ModuleType(name) for name in ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages")}
        modules["app.modules.filemanager.storages.smb"] = fake_storage
        modules["smbclient"] = client
        saved = {}
        plugin = self.plugin.SmbLinkClean()
        plugin._config = {"source": "/源", "library": "/库"}
        plugin.get_data = lambda key: saved.get(key)
        plugin.save_data = lambda key, value: saved.__setitem__(key, value)
        with patch.dict(sys.modules, modules):
            plugin.scan()
            self.assertTrue(saved["status"]["baseline"])
            self.assertEqual(saved["snapshot"]["candidates"], [])
            client.library_ino = 20
            plugin.scan()
            self.assertEqual(saved["snapshot"]["candidates"][0]["reason"], "替换")
            previous = saved["snapshot"]
            client.fail = True
            plugin.scan()
            self.assertIs(saved["snapshot"], previous)
            self.assertFalse(saved["status"]["ok"])

    def test_default_preview_has_no_deletion_calls(self):
        import ast
        tree = ast.parse((PLUGIN / "core.py").read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(node.func, ast.Attribute) and node.func.attr in
                             {"unlink", "remove", "rmdir", "delete"} for node in calls))
        form, defaults = self.plugin.SmbLinkClean().get_form()
        self.assertFalse(defaults["enabled"])
        self.assertFalse(defaults["auto_delete"])

    def test_automatic_delete_waits_for_second_scan_and_ten_minutes(self):
        class Storage:
            def _check_connection(self):
                pass
            def _normalize_path(self, path, writable=False):
                return "\\\\server\\share" + path.replace("/", "\\")

        class Client(types.ModuleType):
            library_ino = 10
            def walk(self, root, **kwargs):
                yield root, [], ["Video.MKV"]
            def lstat(self, path):
                ino = self.library_ino if "\\库\\" in path else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)

        client = Client("smbclient")
        fake_storage = types.ModuleType("app.modules.filemanager.storages.smb")
        fake_storage.SMB = Storage
        modules = {name: types.ModuleType(name) for name in ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages")}
        modules["app.modules.filemanager.storages.smb"] = fake_storage
        modules["smbclient"] = client
        saved, deleted = {}, []
        plugin = self.plugin.SmbLinkClean()
        plugin._config = {"source": "/源", "library": "/库", "enabled": True, "auto_delete": True}
        plugin.get_data = lambda key: saved.get(key)
        plugin.save_data = lambda key, value: saved.__setitem__(key, value)

        def verified(path, expected, recheck):
            self.assertTrue(recheck())
            deleted.append((path, expected))
            return True

        with patch.dict(sys.modules, modules), patch.object(self.plugin, "delete_verified", verified):
            with patch.object(self.plugin.time, "time", return_value=1000):
                plugin.scan()
            client.library_ino = 20
            with patch.object(self.plugin.time, "time", return_value=1001):
                plugin.scan()
            with patch.object(self.plugin.time, "time", return_value=1599):
                plugin.scan()
            self.assertEqual(deleted, [])
            with patch.object(self.plugin.time, "time", return_value=1601):
                plugin.scan()
            self.assertEqual(len(deleted), 1)
            self.assertEqual(saved["status"]["removed"], 1)
            self.assertEqual(saved["snapshot"]["candidates"], [])

    def test_process_restart_rebuilds_baseline(self):
        old = dict(self.before, session="old")
        current = snapshot({"/源/video.mkv": self.source}, {})
        current["session"] = "new"
        self.assertEqual(self.core.calculate(old, current), [])

    def test_empty_library_or_recheck_drop_never_deletes(self):
        class Storage:
            def _check_connection(self): pass
            def _normalize_path(self, path, writable=False):
                return "\\\\server\\share" + path.replace("/", "\\")

        class Client(types.ModuleType):
            mode = "baseline"
            def walk(self, root, **kwargs):
                if "\\库" not in root:
                    yield root, [], ["Video.MKV"]
                elif self.mode == "empty":
                    yield root, [], []
                else:
                    yield root, [], ["Video.MKV"]
            def lstat(self, path):
                ino = 20 if "\\库\\" in path and self.mode != "baseline" else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)

        client = Client("smbclient")
        storage = types.ModuleType("app.modules.filemanager.storages.smb")
        storage.SMB = Storage
        modules = {name: types.ModuleType(name) for name in ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages")}
        modules.update({"app.modules.filemanager.storages.smb": storage, "smbclient": client})
        saved, attempted = {}, []
        plugin = self.plugin.SmbLinkClean()
        plugin._config = {"source": "/源", "library": "/库", "enabled": True, "auto_delete": True}
        plugin.get_data = lambda key: saved.get(key)
        plugin.save_data = lambda key, value: saved.__setitem__(key, value)

        def verified(path, expected, recheck):
            attempted.append(path)
            client.mode = "empty"
            self.assertFalse(recheck())
            return False

        with patch.dict(sys.modules, modules), patch.object(self.plugin, "delete_verified", verified):
            with patch.object(self.plugin.time, "time", return_value=1000): plugin.scan()
            client.mode = "empty"
            with patch.object(self.plugin.time, "time", return_value=1001): plugin.scan()
            with patch.object(self.plugin.time, "time", return_value=1601): plugin.scan()
            self.assertEqual(attempted, [])
            self.assertEqual(len(saved["snapshot"]["candidates"]), 1)
            client.mode = "replacement"
            with patch.object(self.plugin.time, "time", return_value=1602): plugin.scan()
            self.assertEqual(len(attempted), 1)
            self.assertEqual(saved["status"]["removed"], 0)


if __name__ == "__main__":
    unittest.main()
