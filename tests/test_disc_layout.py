import importlib
import tempfile
import unittest
from pathlib import Path
import test_agent_mapping

layout = importlib.import_module("agent_test_package.disc_layout")


class DiscLayoutTest(unittest.TestCase):
    def test_real_download_names_and_ambiguous_names(self):
        for name, expected in [("DISC1", 1), ("disk_02", 2),
                               ("Show.S01.2024.UHD.Disc3-GROUP", 3),
                               ("Disco.2020", None), ("DISC0", None),
                               ("Disc1.Disc2", None)]:
            self.assertEqual(layout.disc_number(Path(name)), expected, name)

    def test_standard_metadata_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "untagged"
            p = root / "BDMV/META/DL/bdmt_eng.xml"; p.parent.mkdir(parents=True)
            p.write_text('<disclib><name>Test Show Season 1 Disc 2</name></disclib>')
            self.assertEqual(layout.disc_number(root), 2)

    def test_real_named_discs_are_scanned_without_renaming(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for n in [1, 2, 3]:
                (root / f"Show.S01.UHD.Disc{n}-Group/BDMV").mkdir(parents=True)
            self.assertEqual(sorted(test_agent_mapping.mapping.discs_in(root)), [1, 2, 3])
