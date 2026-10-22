import importlib
import tempfile
import unittest
from pathlib import Path

from test_core import core
from disc_fixtures import bdjo_blob, index_blob, write_entries

disc = importlib.import_module("core_test_package.disc")


class DiscTest(unittest.TestCase):
    def test_disc_references_determine_jar_not_numeric_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bdmv = root / "BDMV"
            jars = bdmv / "JAR"
            jars.mkdir(parents=True)
            for name in ("00002", "00789", "00321"):
                (jars / (name + ".jar")).write_bytes(b"fixture")
            write_entries(bdmv, ("00789", "00321"), extension="/00321")
            result = disc.discover(root)
            self.assertEqual([Path(j["path"]).stem for j in result["jars"]],
                             ["00789", "00321"])
            self.assertEqual(len(result["descriptors"]), 1)
            self.assertEqual(result["entries"][0]["reference"], "00123")
            self.assertEqual(result["jars"][0]["applications"][0]["class_name"], "example.Menu")

    def test_unregistered_application_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for options in (dict(base="00002"), dict(base="00789/../../outside")):
                write_entries(root / "BDMV", ("00789",), **options)
                with self.assertRaises(ValueError):
                    disc.discover(root)

    def test_truncated_and_misaligned_sections_rejected(self):
        for data in (bdjo_blob()[:47], bdjo_blob()[:-3],
                     bdjo_blob()[:8] + bytes(24) + bdjo_blob()[32:]):
            with self.assertRaises(ValueError):
                disc.bdjo(data)
        for data in (index_blob()[:10], index_blob()[:-1]):
            with self.assertRaises(ValueError):
                disc.index_entries(data)

    def test_hdmv_is_classified_without_claiming_episode_semantics(self):
        entries = disc.index_entries(index_blob(hdmv=True))
        self.assertEqual({e["kind"] for e in entries}, {"HDMV"})
        self.assertEqual(entries[0]["reference"], 0)

    def test_missing_index_never_scans_unregistered_jars(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "BDMV/JAR"
            p.mkdir(parents=True)
            (p / "00002.jar").write_bytes(b"not a valid menu")
            with self.assertRaises(FileNotFoundError):
                disc.discover(directory)


if __name__ == "__main__":
    unittest.main()
