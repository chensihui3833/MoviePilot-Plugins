import importlib.util
import random
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


def fake_entries(files=(), dirs=(), sizes=None):
    sizes = sizes or {}
    return [SimpleNamespace(name=name, smb_info=SimpleNamespace(file_attributes=16 if name in dirs else 0,
                                                                end_of_file=sizes.get(name, 100)),
                            is_symlink=lambda: False) for name in [*dirs, *files]]


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

    def test_randomized_candidates_never_include_a_live_library_identity(self):
        rng = random.Random(20261108)
        for _ in range(500):
            sources = {f"/源/{i}": item(rng.randrange(1, 8), f"/源/{i}") for i in range(6)}
            old_library = {f"/库/{i}": item(rng.randrange(1, 8), f"/库/{i}") for i in range(6)}
            new_library = {f"/库/{i}": item(rng.randrange(1, 8), f"/库/{i}")
                           for i in range(6) if rng.choice((True, False))}
            new_sources = {path: value for path, value in sources.items() if rng.choice((True, False))}
            candidates = self.core.calculate(snapshot(sources, old_library), snapshot(new_sources, new_library))
            live = {self.core.file_key(value) for value in new_library.values()}
            for candidate in candidates:
                self.assertNotIn(self.core.file_key(candidate["identity"]), live)
                self.assertEqual(new_sources[candidate["source_key"]], sources[candidate["source_key"]])

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
            def lstat(self, path):
                return SimpleNamespace(st_mode=stat.S_IFDIR)
            def walk(self, root, **kwargs):
                kwargs["onerror"](OSError("disconnected"))
                yield
        with self.assertRaises(OSError):
            self.plugin.scan_tree(Client(), "\\\\server\\share\\library", "/library")
        class Escape:
            def lstat(self, path):
                return SimpleNamespace(st_mode=stat.S_IFDIR)
            def walk(self, root, **kwargs):
                yield "\\\\server\\other", [], ["x.mkv"]
        with self.assertRaises(ValueError):
            self.plugin.scan_tree(Escape(), "\\\\server\\share\\library", "/library")

    def test_scan_collects_media_and_rejects_symlinks(self):
        class Client:
            linked = False
            def walk(self, root, **kwargs):
                yield root, [], ["Film.MKV", "poster.jpg"] + (["Link.mkv"] if self.linked else [])
            def lstat(self, path):
                mode = stat.S_IFLNK if path.endswith("Link.mkv") else stat.S_IFREG if path.endswith("Film.MKV") else stat.S_IFDIR
                return SimpleNamespace(st_mode=mode, st_dev=1, st_ino=7, st_size=8, st_mtime_ns=9, st_ctime_ns=5)
            def scandir(self, root):
                return fake_entries(["Film.MKV", "poster.jpg"] + (["Link.mkv"] if self.linked else []))
        client = Client()
        result = self.plugin.scan_tree(client, "\\\\server\\share\\library", "/library")
        self.assertEqual(list(result), ["/library/film.mkv"])
        self.assertEqual(result["/library/film.mkv"]["path"], "/library/Film.MKV")
        client.linked = True
        with self.assertRaises(ValueError):
            self.plugin.scan_tree(client, "\\\\server\\share\\library", "/library")

    def test_scan_rejects_linked_parent_and_linked_child_directory(self):
        class Client:
            linked_parent = False
            def walk(self, root, **kwargs):
                yield root, ["redirect"], []
            def lstat(self, path):
                redirect = path.endswith("redirect") or (self.linked_parent and path.endswith("parent"))
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=1 if redirect else 0)
            def scandir(self, root): return fake_entries(dirs=["redirect"])
        client = Client()
        for linked_parent in (False, True):
            client.linked_parent = linked_parent
            with self.subTest(linked_parent=linked_parent), self.assertRaises(ValueError):
                self.plugin.scan_tree(client, "\\\\server\\share\\parent\\library", "/parent/library")

    def test_smb_file_encoded_link_and_changing_listing_abort_scan(self):
        class Client:
            filename = "ordinary"
            size = 1067
            def walk(self, root, **kwargs): yield root, [], [self.filename]
            def scandir(self, root): return fake_entries([self.filename], sizes={self.filename: self.size})
            def lstat(self, path): return SimpleNamespace(st_mode=stat.S_IFDIR)
        client = Client()
        with self.assertRaises(ValueError):
            self.plugin.scan_tree(client, "\\\\server\\share\\library", "/library")
        client.size = 100
        client.scandir = lambda root: fake_entries([])
        with self.assertRaises(OSError):
            self.plugin.scan_tree(client, "\\\\server\\share\\library", "/library")

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
                if not path.endswith("Video.MKV"):
                    return SimpleNamespace(st_mode=stat.S_IFDIR)
                ino = self.library_ino if "\\库\\" in path else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)
            def scandir(self, root): return fake_entries(["Video.MKV"])

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
                if not path.endswith("Video.MKV"):
                    return SimpleNamespace(st_mode=stat.S_IFDIR)
                ino = self.library_ino if "\\库\\" in path else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)
            def scandir(self, root): return fake_entries(["Video.MKV"])

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
            with patch.object(self.plugin.time, "monotonic", return_value=1000):
                plugin.scan()
            client.library_ino = 20
            with patch.object(self.plugin.time, "monotonic", return_value=1001):
                plugin.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1599):
                plugin.scan()
            self.assertEqual(deleted, [])
            with patch.object(self.plugin.time, "monotonic", return_value=1601):
                plugin.scan()
            self.assertEqual(len(deleted), 1)
            self.assertEqual(saved["status"]["removed"], 1)
            self.assertEqual(saved["snapshot"]["candidates"], [])

    def test_process_restart_rebuilds_baseline(self):
        old = dict(self.before, session="old")
        current = snapshot({"/源/video.mkv": self.source}, {})
        current["session"] = "new"
        self.assertEqual(self.core.calculate(old, current), [])

    def test_unreachable_old_directory_cannot_delete_source(self):
        class Storage:
            def _check_connection(self): pass
            def _normalize_path(self, path, writable=False):
                return "\\\\server\\share" + path.replace("/", "\\")

        fake_storage = types.ModuleType("app.modules.filemanager.storages.smb")
        fake_storage.SMB = Storage
        modules = {name: types.ModuleType(name) for name in ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages", "smbclient")}
        modules["app.modules.filemanager.storages.smb"] = fake_storage
        source = item(10)
        lib_old = item(10, "/库/Show/Video.MKV")
        state = {"replacement": False}

        def fake_scan(client, unc, logical, max_files=50000):
            if logical == "/源": return {"/源/video.mkv": source}
            if logical == "/库":
                lib = item(20, lib_old["path"]) if state["replacement"] else lib_old
                return {"/库/show/video.mkv": lib}
            raise OSError("original subdirectory unavailable")

        saved, deleted = {}, []
        plugin = self.plugin.SmbLinkClean()
        plugin._config = {"source": "/源", "library": "/库", "enabled": True, "auto_delete": True}
        plugin.get_data = lambda key: saved.get(key)
        plugin.save_data = lambda key, value: saved.__setitem__(key, value)

        def verified(path, expected, recheck):
            if recheck(): deleted.append(path)
            return bool(deleted)

        with patch.dict(sys.modules, modules), patch.object(self.plugin, "scan_tree", fake_scan), patch.object(self.plugin, "delete_verified", verified):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): plugin.scan()
            state["replacement"] = True
            with patch.object(self.plugin.time, "monotonic", return_value=1001): plugin.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): plugin.scan()
        self.assertEqual(deleted, [])
        self.assertFalse(saved["status"]["ok"])

    def test_partial_failure_reports_confirmed_deletions(self):
        class Storage:
            def _check_connection(self): pass
            def _normalize_path(self, path, writable=False): return "\\\\server\\share" + path.replace("/", "\\")
        fake_storage = types.ModuleType("app.modules.filemanager.storages.smb")
        fake_storage.SMB = Storage
        modules = {name: types.ModuleType(name) for name in ("app", "app.modules", "app.modules.filemanager", "app.modules.filemanager.storages", "smbclient")}
        modules["app.modules.filemanager.storages.smb"] = fake_storage
        a, b = item(10, "/源/A.mkv"), item(10, "/源/B.mkv")
        state = {"replacement": False}
        def fake_scan(client, unc, logical, max_files=50000):
            if logical == "/源": return {"/源/a.mkv": a, "/源/b.mkv": b}
            lib = item(20 if state["replacement"] else 10, "/库/Video.MKV")
            return {"/库/video.mkv": lib}
        saved, attempts = {}, []
        plugin = self.plugin.SmbLinkClean()
        plugin._config = {"source": "/源", "library": "/库", "enabled": True, "auto_delete": True}
        plugin.get_data = lambda key: saved.get(key)
        plugin.save_data = lambda key, value: saved.__setitem__(key, value)
        def verified(path, expected, recheck):
            attempts.append(path)
            if len(attempts) == 2: raise OSError("connection lost")
            return recheck()
        with patch.dict(sys.modules, modules), patch.object(self.plugin, "scan_tree", fake_scan), patch.object(self.plugin, "delete_verified", verified):
            with patch.object(self.plugin.time, "monotonic", return_value=1000): plugin.scan()
            state["replacement"] = True
            with patch.object(self.plugin.time, "monotonic", return_value=1001): plugin.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): plugin.scan()
        self.assertEqual(len(attempts), 2)
        self.assertFalse(saved["status"]["ok"])
        self.assertEqual(saved["status"]["removed"], 1)
        self.assertEqual(len(saved["snapshot"]["candidates"]), 2)

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
                if not path.endswith("Video.MKV"):
                    return SimpleNamespace(st_mode=stat.S_IFDIR)
                ino = 20 if "\\库\\" in path and self.mode != "baseline" else 10
                return SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=ino, st_size=100, st_mtime_ns=1000, st_ctime_ns=500)
            def scandir(self, root):
                return fake_entries([] if "\\库" in root and self.mode == "empty" else ["Video.MKV"])

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
            with patch.object(self.plugin.time, "monotonic", return_value=1000): plugin.scan()
            client.mode = "empty"
            with patch.object(self.plugin.time, "monotonic", return_value=1001): plugin.scan()
            with patch.object(self.plugin.time, "monotonic", return_value=1601): plugin.scan()
            self.assertEqual(attempted, [])
            self.assertEqual(len(saved["snapshot"]["candidates"]), 1)
            client.mode = "replacement"
            with patch.object(self.plugin.time, "monotonic", return_value=1602): plugin.scan()
            self.assertEqual(len(attempted), 1)
            self.assertEqual(saved["status"]["removed"], 0)


if __name__ == "__main__":
    unittest.main()
