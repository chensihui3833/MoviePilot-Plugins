import hashlib
import importlib.util
import json
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "core", Path(__file__).parents[1] / "plugins/blurayepisodes/core.py"
)
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)


def mpls(clip="00334", multi=False, count=1):
    payload = clip.encode() + b"M2TS" + struct.pack(">H", 0x10 if multi else 1) + b"\0"
    payload += struct.pack(">II", 524280, 524280 + 60 * 45000)
    items = (struct.pack(">H", len(payload)) + payload) * count
    playlist = b"\0\0" + struct.pack(">HH", count, 0) + items
    return b"MPLS0200" + struct.pack(">I", 20) + bytes(8) + struct.pack(">I", len(playlist)) + playlist


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.out = self.root / "indexes"
        self.title_names = ["PILOT", "SEE", "THIRD & FINAL"]
        self.make_disc(1, [(1, "00362", "00348"), (2, "00335", "00335")])
        self.make_disc(2, [(1, "00334", "00334")])

    def tearDown(self):
        self.temp.cleanup()

    def make_disc(self, number, items, titles=None):
        bdmv = self.source / f"DISC{number}" / "BDMV"
        for folder in ("JAR/00002", "PLAYLIST", "STREAM"):
            (bdmv / folder).mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bdmv / "JAR/00002.jar", "w") as archive:
            for index, title in enumerate(titles or self.title_names, 1):
                archive.writestr(
                    f"eng/tblEP_T3_{index:02}_res.qct",
                    b"QCTF\0TITLE\0" + title.encode() + b"\0Menu Title\0",
                )
        xml = ET.Element("playlist-metadata")
        for local, pid, clip in reversed(items):
            ET.SubElement(xml, "playlist", name=f"EP {local}", id=pid, duration_ms="60000")
            (bdmv / "PLAYLIST" / (pid + ".mpls")).write_bytes(mpls(clip))
            (bdmv / "STREAM" / (clip + ".m2ts")).write_bytes(b"test-video-" + clip.encode())
        # Duplicate language variant is deliberately not a primary EP entry.
        ET.SubElement(xml, "playlist", name="_EP 1 A", id="00999", duration_ms="60000")
        ET.ElementTree(xml).write(bdmv / "JAR/00002/playlists.xml")

    def scan(self, **kwargs):
        options = dict(
            source_dir=self.source, series="Test Show", season=1, output_root=self.out,
            ffprobe="stub",
        )
        options.update(kwargs)
        with patch.object(core, "probe_video", return_value=(524280 / 45000, 60)):
            return core.scan_season(**options)

    def source_hashes(self):
        return {str(p.relative_to(self.source)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.source.rglob("*") if p.is_file()}

    def test_uses_menu_order_not_playlist_filename_and_deduplicates(self):
        before = self.source_hashes()
        plan = self.scan()
        self.assertEqual([r["playlist"] for r in plan["episodes"]], ["00362", "00335", "00334"])
        self.assertEqual([r["clip"] for r in plan["episodes"]], ["00348", "00335", "00334"])
        self.assertEqual([r["title"] for r in plan["episodes"]], self.title_names)
        self.assertEqual(before, self.source_hashes())
        self.assertFalse(self.out.exists())

    def test_export_is_additive_idempotent_and_preserves_originals(self):
        before = self.source_hashes()
        plan = self.scan()
        self.assertEqual(core.export_plan(plan, plan["fingerprint"])["created_files"], 7)
        self.assertEqual(core.export_plan(plan, plan["fingerprint"])["created_files"], 0)
        nfo = Path(plan["output"]) / "Season 1/Test Show - S01E03.nfo"
        self.assertEqual(ET.parse(nfo).findtext("title"), "THIRD & FINAL")
        self.assertEqual(before, self.source_hashes())
        for relative, row in core.render_links(plan).items():
            self.assertTrue((Path(plan["output"]) / relative).samefile(row["video"]))
            self.assertGreaterEqual(Path(row["video"]).stat().st_nlink, 2)

    def test_changed_source_and_foreign_video_are_not_overwritten(self):
        plan = self.scan()
        Path(plan["episodes"][0]["video"]).write_bytes(b"changed")
        with self.assertRaises(core.NeedsReview):
            core.export_plan(plan, plan["fingerprint"])
        self.assertFalse(self.out.exists())
        plan = self.scan()
        relative = next(iter(core.render_links(plan)))
        target = Path(plan["output"]) / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"another-video")
        with self.assertRaises(core.NeedsReview):
            core.export_plan(plan, plan["fingerprint"])
        self.assertEqual(target.read_bytes(), b"another-video")
        self.assertFalse((Path(plan["output"]) / "tvshow.nfo").exists())

    def test_nonfinite_probe_times_rejected(self):
        for start, duration in ((float("nan"), 60), (0, float("inf"))):
            with patch.object(core, "probe_video", return_value=(start, duration)):
                with self.assertRaises(core.NeedsReview):
                    core.scan_season(self.source, "Test", 1, self.out, ffprobe="stub")

    def test_output_conflict_stops_before_other_writes(self):
        plan = self.scan()
        target = Path(plan["output"])
        target.mkdir(parents=True)
        (target / "tvshow.nfo").write_text("user-owned")
        with self.assertRaises(core.NeedsReview):
            core.export_plan(plan, plan["fingerprint"])
        self.assertEqual((target / "tvshow.nfo").read_text(), "user-owned")
        self.assertEqual(len(list(target.rglob("*"))), 1)

    def test_stale_preview_and_missing_preview_cannot_export(self):
        plan = self.scan()
        for fingerprint in (None, "old-preview"):
            with self.assertRaises(core.NeedsReview):
                core.export_plan(plan, fingerprint)
        self.assertFalse(self.out.exists())

    def test_missing_disc_rejected(self):
        (self.source / "DISC2").rename(self.source / "DISC3")
        with self.assertRaises(core.NeedsReview):
            self.scan()

    def test_partial_season_rejected(self):
        self.title_names.append("MISSING EPISODE")
        self.make_disc(1, [(1, "00362", "00348"), (2, "00335", "00335")])
        self.make_disc(2, [(1, "00334", "00334")])
        with self.assertRaises(core.NeedsReview):
            self.scan()

    def test_conflicting_titles_rejected(self):
        self.make_disc(2, [(1, "00334", "00334")], titles=["WRONG", "SEE", "THIRD"])
        with self.assertRaises(core.NeedsReview):
            self.scan()

    def test_multiple_segments_rejected(self):
        path = self.source / "DISC1/BDMV/PLAYLIST/00362.mpls"
        path.write_bytes(mpls("00348", count=2))
        with self.assertRaises(core.NeedsReview):
            self.scan()

    def test_multi_angle_and_truncated_playlist_rejected(self):
        for data in (mpls(multi=True), mpls()[:-1], b"MPLS"):
            with self.assertRaises(core.NeedsReview):
                core.playlist_items(data)

    def test_trimmed_stream_rejected(self):
        with patch.object(core, "probe_video", return_value=(0, 120)):
            with self.assertRaises(core.NeedsReview):
                core.scan_season(self.source, "Test", 1, self.out, ffprobe="stub")

    def test_xml_entities_rejected(self):
        path = self.source / "DISC1/BDMV/JAR/00002/playlists.xml"
        path.write_text('<!DOCTYPE x [<!ENTITY a "x">]><playlist-metadata/>')
        with self.assertRaises(core.NeedsReview):
            self.scan()

    def test_source_output_overlap_and_blank_paths_rejected(self):
        for options in ({"output_root": self.source / "index"}, {"output_root": self.root},
                        {"output_root": ""}, {"source_dir": ""}, {"series": "../bad"}):
            with self.assertRaises(core.NeedsReview):
                self.scan(**options)


if __name__ == "__main__":
    unittest.main()
