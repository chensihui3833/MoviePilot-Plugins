import copy
import importlib
import sys
import unittest
import types
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_adapter import adapter
import test_native as native_fixture

package = types.ModuleType("episode_task_test_package")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/blurayepisodes")]
sys.modules[package.__name__] = package
ep = importlib.import_module(package.__name__ + ".episode_adapter")
native = importlib.import_module(package.__name__ + ".native")
native_core = importlib.import_module(package.__name__ + ".core")
mapping = importlib.import_module(package.__name__ + ".agent_mapping")
bridge = importlib.import_module(package.__name__ + ".agent_bridge")


class EpisodeTaskTest(unittest.TestCase):
    def setUp(self):
        self.f = native_fixture.NativeTest(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.plugin = self.f.plugin
        self.plugin._episode_tasks = True
        self.plugin.get_data = self.plugin.saved.get
        self.parent = NS(type="TV", name="Test Show", begin_season=1, end_season=None,
                         begin_episode=None, end_episode=None)
        self.mapping = {"season":1, "unknown":[], "basis":"menu actions identify episodes", 
                        "evidence":["DISC1/BDMV/JAR/00002/playlists.xml"], "episodes":[
                            {"episode":1,"disc":1,"title":"First","playlist":"DISC1/BDMV/PLAYLIST/00362.mpls"},
                            {"episode":2,"disc":1,"title":"Second","playlist":"DISC1/BDMV/PLAYLIST/00335.mpls"},
                            {"episode":3,"disc":2,"title":"Third","playlist":"DISC2/BDMV/PLAYLIST/00334.mpls"}]}
        parent = self.parent
        class Planner:
            def __init__(self):
                self._chain = NS(_get_file_key=lambda item:(item.storage or "local", str(Path(item.path))))
                self._continue_callback = None
                self._meta = None
            def _plan_file_items(self, items):
                return list(items), {}
            def _build_file_meta(self, item):
                return copy.deepcopy(self._meta or parent)
            def _build_path_meta(self, *args, **kwargs):
                return copy.deepcopy(parent)
            def _build_file_context(self, file, history, inherited, discard):
                return NS(begin_episode=999), None
        self.Planner = Planner
        self.restore = ep.install(self.plugin, Planner); self.addCleanup(self.restore)
        self.ctx = patch.dict(sys.modules, self.f.host); self.ctx.start(); self.addCleanup(self.ctx.stop)
        for target, name, value in ((mapping, "probe_video", (524280/45000,60)),
                                    (native_core, "probe_video", (524280/45000,60)),
                                    (mapping.shutil, "which", "stub")):
            p = patch.object(target,name,return_value=value); p.start(); self.addCleanup(p.stop)
        ai_patch = patch.object(bridge,"analyze",return_value=self.mapping)
        self.ai = ai_patch.start(); self.addCleanup(ai_patch.stop)

    def disc_items(self):
        return [(NS(storage="local",type="dir",path=str(self.f.source / f"DISC{n}")),True) for n in (1,2)]

    def planned(self):
        planner = self.Planner()
        items, inherited = planner._plan_file_items(self.disc_items())
        return planner, items, inherited

    def transfer(self, planner, file, preview=False):
        meta, _ = planner._build_file_context(file,None,None,False)
        return native.native_transfer(self.plugin,file,meta,self.f.media,target_path=self.f.out,preview=preview)

    def test_disc_inputs_become_independent_real_video_tasks_and_preview_no_writes(self):
        planner = self.Planner(); planner._meta = NS(**vars(self.parent)); planner._meta.begin_episode = 99
        items, inherited = planner._plan_file_items(self.disc_items())
        self.assertEqual(len(items),3)
        self.assertEqual([Path(f.path).name for f,_ in items],["00348.m2ts","00335.m2ts","00334.m2ts"])
        for n,(file,is_disc) in enumerate(items,1):
            self.assertFalse(is_disc); self.assertEqual(file.type,"file")
            self.assertGreater(file.size,0)
            meta,_ = planner._build_file_context(file,None,None,False)
            self.assertEqual(meta.begin_episode,n)
            result = self.transfer(planner,file,True)
            self.assertTrue(result.success,result.message)
            self.assertEqual(result.file_count,1)
            self.assertEqual(len(result.file_list_new),1)
        self.assertEqual(self.ai.call_count,1)
        self.assertFalse(self.f.out.exists())

    def test_one_episode_conflict_isolated_and_single_retry_does_not_touch_others(self):
        before = self.f.source_hashes()
        planner,items,_ = self.planned()
        conflict = self.f.out / "Test Show/Season 1/Test Show - S01E02.m2ts"
        conflict.parent.mkdir(parents=True); conflict.write_bytes(b"different video")
        results = [self.transfer(planner,file) for file,_ in items]
        self.assertEqual([r.success for r in results],[True,False,True])
        self.assertEqual(conflict.read_bytes(),b"different video")
        first,third = Path(results[0].file_list_new[0]),Path(results[2].file_list_new[0])
        stamps = [(p.stat().st_ino,p.stat().st_mtime_ns) for p in (first,third)]
        conflict.unlink()  # disposable fixture collision only
        retry = self.Planner(); retry._meta = copy.deepcopy(self.parent); retry._meta.begin_episode=2
        result = self.transfer(retry,items[1][0])
        self.assertTrue(result.success,result.message)
        self.assertTrue(conflict.samefile(items[1][0].path))
        self.assertEqual(stamps,[(p.stat().st_ino,p.stat().st_mtime_ns) for p in (first,third)])
        self.assertEqual(before,self.f.source_hashes())
        self.assertEqual(self.ai.call_count,1)

    def test_host_disc_promotion_restores_only_explicit_stream(self):
        planner, items, _ = self.planned()
        retry = self.Planner(); retry._fileitem = items[1][0]
        planned, inherited = retry._plan_file_items([self.disc_items()[0]])
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0][0].path, items[1][0].path)
        self.assertFalse(planned[0][1])
        meta, _ = retry._build_file_context(planned[0][0], None, None, False)
        self.assertEqual(meta.begin_episode, 2)

    def test_unmapped_video_and_wrong_episode_retry_rejected(self):
        planner,items,_ = self.planned()
        planner = self.Planner(); planner._meta=copy.deepcopy(self.parent); planner._meta.begin_episode=3
        result = self.transfer(planner,items[0][0])
        self.assertFalse(result.success)
        self.assertIn("季集号",result.message)
        extra = self.f.source / "DISC1/BDMV/STREAM/00999.m2ts"; extra.write_bytes(b"extra")
        result = self.transfer(self.Planner(),NS(storage="local",type="file",path=str(extra)))
        self.assertFalse(result.success)
        self.assertIn("源视频",result.message)

    def test_disable_passthrough_and_reload_restore(self):
        original = self.Planner._plan_file_items._blurayepisodes_original
        old_restore=self.restore
        new_restore=ep.install(self.plugin,self.Planner)
        old_restore()
        self.assertIs(self.Planner._plan_file_items._blurayepisodes_original,original)
        self.plugin.get_state=lambda:False
        items,_=self.Planner()._plan_file_items(self.disc_items())
        self.assertTrue(all(is_disc for _,is_disc in items))
        new_restore(); self.assertIs(self.Planner._plan_file_items,original)

    def test_unknown_mapping_keeps_explicit_disc_failure_never_invents_episodes(self):
        self.mapping["unknown"]=["not confirmed"]
        planner,items,_=self.planned()
        self.assertEqual(len(items),2)
        self.assertTrue(all(is_disc for _,is_disc in items))
        result = native.native_transfer(self.plugin,items[0][0],self.parent,self.f.media,target_path=self.f.out)
        self.assertFalse(result.success)
        self.assertIn("未确认",result.message)
        self.assertFalse(self.f.out.exists())
