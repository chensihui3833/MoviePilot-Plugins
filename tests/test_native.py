import importlib
import sys
import types
import unittest
from pathlib import Path
from threading import Lock
from unittest.mock import patch

from test_adapter import adapter
import test_core

with patch.dict(sys.modules, {"blurayepisodes": adapter}):
    native = importlib.import_module("blurayepisodes.native")
    native_core = importlib.import_module("blurayepisodes.core")
NS = types.SimpleNamespace


class NativeTest(unittest.TestCase):
    """Exercise the complete provider using fixture discs and isolated host services."""

    make_disc = test_core.CoreTest.make_disc
    source_hashes = test_core.CoreTest.source_hashes
    tearDown = test_core.CoreTest.tearDown

    def setUp(self):
        test_core.CoreTest.setUp(self)
        self.meta = NS(begin_season=1, end_season=None, begin_episode=None)
        self.media = NS(type="TV", title_year="Test Show", tmdb_id="123")
        self.file = NS(path=str(self.source / "DISC1"), storage="local", type="dir")
        self.plugin = NS(
            get_state=lambda: True, _run_lock=Lock(), saved={},
            _preview_plans={},
            chain=NS(run_module=lambda method, **kw: self.naming(**kw)),
        )
        self.plugin.save_data = lambda k, v: self.plugin.saved.update({k: v})
        self.stop = NS(is_system_stopped=False, consume_transfer_stop=lambda path: False)
        self.host = {
            "app.schemas.transfer": NS(TransferInfo=lambda **kw: NS(**kw)),
            "app.schemas.file": NS(FileItem=lambda **kw: NS(**kw)),
            "app.schemas.types": NS(MediaType=NS(TV="TV")),
            "app.modules.filemanager.transhandler": NS(TransHandler=NS(
                get_dest_path=lambda media, path, *args: path,
                get_dest_dir=lambda media, directory, *args: Path(directory.library_path),
            )),
            "app.application.directory": NS(DirectoryHelper=NS(
                get_media_root_path=lambda template, rename_path, media_type: rename_path.parent.parent,
            )),
            "app.runtime.settings": NS(get_runtime_setting=lambda name: lambda kind: "{{title}}/Season {{season}}/file"),
            "app.runtime.stop": NS(runtime_stop_state=self.stop),
        }

    @staticmethod
    def naming(meta, **kwargs):
        return f"Test Show/Season 1/Test Show - S01E{meta.begin_episode:02}.m2ts"

    def transfer(self, **kwargs):
        options = dict(target_path=self.out, preview=False)
        options.update(kwargs)
        with patch.dict(sys.modules, self.host), \
             patch.object(native_core.shutil, "which", return_value="stub"), \
             patch.object(native_core, "probe_video", return_value=(524280 / 45000, 60)):
            return native.native_transfer(self.plugin, self.file, self.meta, self.media, **options)

    def test_native_preview_no_writes_then_per_disc_idempotent(self):
        before = self.source_hashes()
        preview = self.transfer(preview=True)
        self.assertTrue(preview.success, getattr(preview, "message", ""))
        self.assertFalse(self.out.exists())
        result = self.transfer()
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.transfer_type, "link")
        self.assertEqual(result.target_item.type, "file")
        self.assertEqual(len(result.file_list_new), 2)
        self.assertTrue(self.transfer().success)
        self.file.path = str(self.source / "DISC2")
        result = self.transfer()
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.file_count, 1)
        self.assertIn("S01E03", result.file_list_new[0])
        for nfo in self.out.rglob("*.nfo"):
            self.assertEqual(nfo.stem, nfo.with_suffix(".m2ts").stem)
        self.assertEqual(len(list(self.out.rglob("*.m2ts"))), 3)
        self.assertEqual(before, self.source_hashes())

    def test_toggle_and_non_tv_passthrough(self):
        self.plugin.get_state = lambda: False
        self.assertIsNone(self.transfer())
        self.plugin.get_state = lambda: True
        self.media.type = "MOVIE"
        self.assertIsNone(self.transfer())

    def test_unsupported_tv_disc_fails_without_fallback(self):
        (self.source / "DISC1/BDMV/JAR/00002.jar").rename(self.source / "menu-original.jar")
        result = self.transfer()
        self.assertFalse(result.success)
        self.assertIn("待核对", result.message)
        self.assertFalse(self.out.exists())

    def test_existing_video_conflict_preserves_source_and_target(self):
        target = self.out / "Test Show/Season 1/Test Show - S01E02.m2ts"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"another-video")
        before = self.source_hashes()
        result = self.transfer()
        self.assertFalse(result.success)
        self.assertEqual(target.read_bytes(), b"another-video")
        self.assertEqual(before, self.source_hashes())
        self.assertFalse(target.with_name("Test Show - S01E01.m2ts").exists())

    def test_cancel_after_first_link_stops_and_retry_completes(self):
        self.stop.consume_transfer_stop = lambda path: len(list(self.out.rglob("*.m2ts"))) > 0
        result = self.transfer()
        self.assertFalse(result.success)
        self.assertEqual(len(list(self.out.rglob("*.m2ts"))), 1)
        self.stop.consume_transfer_stop = lambda path: False
        self.assertTrue(self.transfer().success)
        self.assertEqual(len(list(self.out.rglob("*.m2ts"))), 2)

    def test_remote_target_or_no_season_is_explicit_failure(self):
        self.assertFalse(self.transfer(target_storage="alist").success)
        self.meta.begin_season = None
        self.assertFalse(self.transfer().success)
        self.assertFalse(self.out.exists())

    def test_host_directory_selection_and_forced_hardlink(self):
        directory = NS(library_path=str(self.out), library_storage="local",
                       renaming=True, scraping=True, notify=True)
        result = self.transfer(target_directory=directory, transfer_type="move")
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.transfer_type, "link")
        self.assertTrue(result.need_scrape)
        self.assertTrue(Path(result.file_list[0]).samefile(result.file_list_new[0]))
        directory.renaming = False
        self.assertFalse(self.transfer(target_directory=directory).success)

    def test_enriched_episode_nfo_is_preserved_on_retry(self):
        self.assertTrue(self.transfer().success)
        path = next(self.out.rglob("*.nfo"))
        content = path.read_text(encoding="utf-8").replace(
            "</episodedetails>", "<plot>Emby metadata</plot></episodedetails>"
        )
        path.write_text(content, encoding="utf-8")
        self.assertTrue(self.transfer().success)
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        path.write_text(content.replace("<season>1</season>", "<season>9</season>"), encoding="utf-8")
        self.assertFalse(self.transfer().success)

    def test_invalid_naming_fails_before_any_write(self):
        for name in ("../escape.m2ts", "Test Show/episode.m2ts", "Test Show/S01E01.mp4"):
            self.plugin.chain.run_module = lambda *args, **kw: name
            self.assertFalse(self.transfer().success, name)
            self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
