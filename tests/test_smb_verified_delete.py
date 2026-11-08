import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


FILE = Path(__file__).parents[1] / "plugins.v3/smblinkclean/verified_delete.py"
spec = importlib.util.spec_from_file_location("verified_delete_test", FILE)
verified_delete = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verified_delete)


class Field:
    def __init__(self, value):
        self.value = value
    def get_value(self):
        return self.value


class VerifiedDeleteTests(unittest.TestCase):
    def setUp(self):
        self.operations = []
        self.values = {"dev": 8, "ino": 17, "size": 100, "mtime_ns": 500, "ctime_ns": 700, "tag": 0, "attrs": 0}
        self.expected = {k: self.values[k] for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}
        outer = self
        io = types.ModuleType("smbclient._io")
        info = types.ModuleType("smbprotocol.file_info")
        opened = types.ModuleType("smbprotocol.open")
        structure = types.ModuleType("smbprotocol.structure")
        for name in ("FileAttributeTagInformation", "FileBasicInformation", "FileFsVolumeInformation",
                     "FileInternalInformation", "FileStandardInformation"):
            setattr(info, name, type(name, (), {}))
        info.FileDispositionInformation = type("FileDispositionInformation", (dict,), {})
        info.FileAttributes = types.SimpleNamespace(FILE_ATTRIBUTE_REPARSE_POINT=1024)
        opened.CreateOptions = types.SimpleNamespace(FILE_OPEN_REPARSE_POINT=1, FILE_NON_DIRECTORY_FILE=2)
        opened.FilePipePrinterAccessMask = types.SimpleNamespace(DELETE=4, FILE_READ_ATTRIBUTES=8)
        structure.DateTimeField = types.SimpleNamespace(EPOCH_FILETIME=100)

        class Raw:
            def __init__(self, path, **kwargs):
                outer.operations.append(("create", path, kwargs))
            def open(self):
                outer.operations.append("open")
            def close(self):
                outer.operations.append("close")

        class Transaction:
            def __init__(self, raw):
                self.results = None
                self.queries = []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                if self.queries:
                    v = outer.values
                    self.results = [
                        {"last_write_time": Field(100 + v["mtime_ns"] // 100),
                         "creation_time": Field(100 + v["ctime_ns"] // 100), "file_attributes": Field(v["attrs"])},
                        {"volume_serial_number": Field(v["dev"])},
                        {"index_number": Field(v["ino"])}, {"end_of_file": Field(v["size"])},
                        {"reparse_tag": Field(v["tag"])},
                    ]

        io.SMBFileIO = Raw
        io.SMBFileTransaction = Transaction
        io.query_info = lambda transaction, cls, **kwargs: transaction.queries.append(cls)
        io.set_info = lambda transaction, value: outer.operations.append(("delete", dict(value)))
        modules = {name: types.ModuleType(name) for name in ("smbclient", "smbprotocol")}
        modules.update({"smbclient._io": io, "smbprotocol.file_info": info,
                        "smbprotocol.open": opened, "smbprotocol.structure": structure})
        self.patcher = patch.dict(sys.modules, modules)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_exact_identity_and_library_recheck_delete_on_same_handle(self):
        self.assertTrue(verified_delete.delete_verified("/test/source", self.expected, lambda: True))
        self.assertEqual(self.operations[-2:], [("delete", {"delete_pending": True}), "close"])
        self.assertEqual(self.operations[0][2]["share_access"], "r")

    def test_mismatched_identity_and_reparse_points_are_never_deleted(self):
        for field, value in (("ino", 18), ("dev", 9), ("size", 101), ("mtime_ns", 600),
                             ("ctime_ns", 800), ("tag", 1), ("attrs", 1024)):
            with self.subTest(field=field):
                self.values[field] = value
                self.assertFalse(verified_delete.delete_verified("/test/source", self.expected, lambda: True))
                self.assertFalse(any(isinstance(op, tuple) and op[0] == "delete" for op in self.operations))
                self.values[field] = self.expected.get(field, 0)
                self.operations.clear()

    def test_failed_or_exceptional_library_recheck_never_deletes(self):
        self.assertFalse(verified_delete.delete_verified("/test/source", self.expected, lambda: False))
        self.assertEqual(self.operations[-1], "close")
        self.assertFalse(any(isinstance(op, tuple) and op[0] == "delete" for op in self.operations))
        self.operations.clear()
        def fail():
            raise ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            verified_delete.delete_verified("/test/source", self.expected, fail)
        self.assertEqual(self.operations[-1], "close")
        self.assertFalse(any(isinstance(op, tuple) and op[0] == "delete" for op in self.operations))


if __name__ == "__main__":
    unittest.main()
