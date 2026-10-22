import importlib
import struct
import unittest
from test_agent_mapping import package
import test_core

guard = importlib.import_module(package.__name__ + ".playlist_guard")


def with_subpath(kind=10, clip="00334", delta=0, flags=2, repeat=0, sync_id=0, sync_shift=0):
    data = bytearray(test_core.mpls())
    start = 20
    struct.pack_into(">H", data, start + 8, 1)
    pts = 524280
    item = clip.encode() + b"M2TS" + struct.pack(">IBIIHI", flags, 0, pts,
                            pts + 60 * 45000 + delta, sync_id, pts + sync_shift)
    sub = bytes([0, kind]) + struct.pack(">H", repeat) + bytes([0, 1])
    sub += struct.pack(">H", len(item)) + item
    data += struct.pack(">I", len(sub)) + sub
    struct.pack_into(">I", data, start, len(data) - start - 4)
    return data


class PlaylistGuardTest(unittest.TestCase):
    def test_same_file_dv_layer_and_tick_rounding_are_preserved(self):
        for delta in (-1, 0, 1):
            self.assertEqual(guard.validate_subpaths(with_subpath(delta=delta)),
                             guard.playlist_items(test_core.mpls()))

    def test_other_file_range_type_repeat_or_sync_rejected(self):
        for values in ({"kind": 7}, {"clip": "00099"}, {"delta": 2},
                       {"flags": 3}, {"repeat": 1}, {"sync_id": 1},
                       {"sync_shift": 1}):
            with self.subTest(values=values), self.assertRaises(guard.NeedsReview):
                guard.validate_subpaths(with_subpath(**values))

    def test_truncated_and_extra_subpath_data_rejected(self):
        original = with_subpath()
        for value in (original[:-1], original[:30]):
            with self.assertRaises(guard.NeedsReview):
                guard.validate_subpaths(value)
        value = original + b"x"
        struct.pack_into(">I", value, 20, len(value) - 24)
        with self.assertRaises(guard.NeedsReview):
            guard.validate_subpaths(value)
