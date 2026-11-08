import importlib.util
import os
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1] / 'plugins.v3/locallinkclean'


def load_plugin():
    modules = {name: types.ModuleType(name) for name in ('app', 'app.runtime', 'app.runtime.log', 'app.sdk', 'app.sdk.plugin', 'app.sdk.plugin.base')}
    modules['app.runtime.log'].logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    modules['app.sdk.plugin.base']._PluginBase = object
    spec = importlib.util.spec_from_file_location('local_clean_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    modules['local_clean_test'] = plugin
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
    return plugin


class FakeQB:
    def __init__(self, files):
        self.files = sorted(files)
        self.paused = False
        self.removed = False
    def tasks_for(self, candidates, known_retired=None):
        return [] if self.removed or not (set(candidates) & set(self.files)) else [{'hash': 'a' * 40, 'files': self.files}]
    def pause(self, task): self.paused = True; return True
    def verify_task(self, task): return self.paused and not self.removed
    def remove(self, task): self.removed = self.verify_task(task); return self.removed
    def get_task(self, tid): return None if self.removed else {'hash': tid, 'files': self.files}


class LocalCleanTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source, self.library = self.root/'source', self.root/'library'
        self.source.mkdir(); self.library.mkdir()
        self.old33, self.old34 = self.source/'33.mkv', self.source/'34.mkv'
        self.lib33, self.lib34 = self.library/'33.mkv', self.library/'34.mkv'
        self.old33.write_bytes(b'old33'); self.old34.write_bytes(b'old34')
        os.link(self.old33, self.lib33); os.link(self.old34, self.lib34)
    def tearDown(self): self.tmp.cleanup()
    def replace(self, library):
        tmp = library.with_suffix('.tmp')
        tmp.write_bytes(b'new version')
        os.replace(tmp, library)
    def test_native_hardlink_replacement_creates_candidate_despite_ctime_change(self):
        prev = {'scope': 'x', 'session': 's', 'sources': self.plugin.scan_tree(str(self.source)), 'library': self.plugin.scan_tree(str(self.library)), 'epoch': 1}
        self.replace(self.lib33)
        current = {'scope': 'x', 'session': 's', 'sources': self.plugin.scan_tree(str(self.source)), 'library': self.plugin.scan_tree(str(self.library)), 'epoch': 2}
        candidates = self.plugin.calculate(prev, current)
        self.assertEqual([x['source'] for x in candidates], [str(self.old33)])
    def test_symlink_fails_closed(self):
        (self.library/'link').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError): self.plugin.scan_tree(str(self.library))
    def test_verified_delete_requires_matching_identity_and_recheck(self):
        original = {'path': str(self.old33), **self.plugin.local_identity(os.lstat(self.old33))}
        self.assertFalse(self.plugin.delete_verified(str(self.old33), original, lambda: False))
        changed = {**original, 'ino': original['ino'] + 1}
        self.assertFalse(self.plugin.delete_verified(str(self.old33), changed, lambda: True))
        self.assertTrue(self.old33.exists())
        self.assertTrue(self.plugin.delete_verified(str(self.old33), original, lambda: True))
        self.assertFalse(self.old33.exists()); self.assertTrue(self.lib33.exists())
    def test_file_replacement_during_recheck_is_not_deleted(self):
        original = {'path': str(self.old33), **self.plugin.local_identity(os.lstat(self.old33))}
        def replace_source():
            self.replace(self.old33)
            return True
        self.assertFalse(self.plugin.delete_verified(str(self.old33), original, replace_source))
        self.assertEqual(self.old33.read_bytes(), b'new version')

    def test_missing_torrent_retains_old_source(self):
        instance = self.plugin.LocalLinkClean(); saved = {}
        instance._config = {'enabled': True, 'auto_delete': True, 'qb_names': ['local QB'], 'pairs': f'{self.source}|{self.library}'}
        instance.get_data = lambda key: deepcopy(saved.get(key))
        instance.save_data = lambda key, value: saved.__setitem__(key, deepcopy(value))
        qb = FakeQB([])
        with patch.object(self.plugin, 'existing_qb', lambda name: qb):
            with patch.object(self.plugin.time, 'monotonic', return_value=1000): instance.scan()
            self.replace(self.lib33)
            with patch.object(self.plugin.time, 'monotonic', return_value=1001): instance.scan()
            with patch.object(self.plugin.time, 'monotonic', return_value=1601): instance.scan()
        self.assertEqual(self.old33.read_bytes(), b'old33')
        self.assertEqual(saved['status']['skipped'], 1)
        self.assertFalse(qb.paused)
    def test_real_bridge_matches_mapped_local_save_path(self):
        tid = 'b' * 40
        class Client:
            def get_torrents(self, ids=None):
                return ([{'hash': tid, 'save_path': '/downloads', 'state': 'uploading', 'progress': 1.0}], False)
            def get_files(self, tid):
                return [{'name': 'pack/33.mkv'}, {'name': 'pack/34.mkv'}]
        client = Client()
        class Module:
            def get_instance(self, name): return client
            def normalize_return_path(self, path, name): return '/video/源数据/电视剧'
        qb = self.plugin.qb_bridge.QBBridge(Module(), 'local QB')
        tasks = qb.tasks_for(['/video/源数据/电视剧/pack/33.mkv'])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['files'], ['/video/源数据/电视剧/pack/33.mkv', '/video/源数据/电视剧/pack/34.mkv'])
    def test_real_hardlinks_two_episode_pause_then_remove_task(self):
        instance = self.plugin.LocalLinkClean(); saved = {}
        instance._config = {'enabled': True, 'auto_delete': True, 'qb_names': ['local QB'], 'pairs': f'{self.source}|{self.library}'}
        instance.get_data = lambda key: deepcopy(saved.get(key))
        instance.save_data = lambda key, value: saved.__setitem__(key, deepcopy(value))
        qb = FakeQB([str(self.old33), str(self.old34)])
        with patch.object(self.plugin, 'existing_qb', lambda name: qb):
            with patch.object(self.plugin.time, 'monotonic', return_value=1000): instance.scan()
            self.replace(self.lib33)
            with patch.object(self.plugin.time, 'monotonic', return_value=1001): instance.scan()
            with patch.object(self.plugin.time, 'monotonic', return_value=1601): instance.scan()
            self.assertFalse(self.old33.exists()); self.assertTrue(self.old34.exists())
            self.assertTrue(qb.paused); self.assertFalse(qb.removed)
            self.replace(self.lib34)
            with patch.object(self.plugin.time, 'monotonic', return_value=1602): instance.scan()
            with patch.object(self.plugin.time, 'monotonic', return_value=2202): instance.scan()
            self.assertFalse(self.old34.exists()); self.assertTrue(qb.removed)
            self.assertEqual(self.lib33.read_bytes(), b'new version'); self.assertEqual(self.lib34.read_bytes(), b'new version')
            self.assertTrue(saved['status']['ok'])

    def test_previously_missing_pack_file_does_not_block_final_torrent_removal(self):
        instance = self.plugin.LocalLinkClean(); saved = {}
        instance._config = {'enabled': True, 'auto_delete': True, 'qb_names': ['local QB'], 'pairs': f'{self.source}|{self.library}'}
        instance.get_data = lambda key: deepcopy(saved.get(key))
        instance.save_data = lambda key, value: saved.__setitem__(key, deepcopy(value))
        # 模拟整季中 38 集从未选中或已手工清理，只有两集源文件还存在。
        missing = [str(self.source / f'absent-{n:02d}.mkv') for n in range(1, 39)]
        qb = FakeQB([*missing, str(self.old33), str(self.old34)])
        with patch.object(self.plugin, 'existing_qb', lambda name: qb):
            with patch.object(self.plugin.time, 'monotonic', return_value=1000): instance.scan()
            self.replace(self.lib33)
            with patch.object(self.plugin.time, 'monotonic', return_value=1001): instance.scan()
            with patch.object(self.plugin.time, 'monotonic', return_value=1601): instance.scan()
            self.assertFalse(self.old33.exists()); self.assertTrue(self.old34.exists())
            self.assertTrue(qb.paused); self.assertFalse(qb.removed)
            self.replace(self.lib34)
            with patch.object(self.plugin.time, 'monotonic', return_value=1602): instance.scan()
            with patch.object(self.plugin.time, 'monotonic', return_value=2202): instance.scan()
            self.assertFalse(self.old34.exists()); self.assertTrue(qb.removed)
            self.assertEqual(self.lib33.read_bytes(), b'new version')
            self.assertEqual(self.lib34.read_bytes(), b'new version')


if __name__ == '__main__': unittest.main()
