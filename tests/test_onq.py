"""Synthetic menu programs; no commercial disc program or resources are bundled."""
import importlib
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from test_core import core

onq = importlib.import_module("core_test_package.onq")
binary = importlib.import_module("core_test_package.onq_binary")


def table_blob(rows, text=False):
    cols = len(rows[0])
    offsets = 32
    types = offsets + cols * 2
    data = types + cols
    stride = cols * 2
    payload = bytearray(len(rows) * stride)
    strings = bytearray()
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if text:
                value_offset = data + len(payload) + len(strings)
                strings.extend(str(value).encode() + b"\0")
            else:
                value_offset = value
            struct.pack_into(">H", payload, r * stride + c * 2, value_offset)
    header = (b"QCTF" if text else b"QCSF") + struct.pack(
        ">6I2H", 1, len(rows), cols, types, offsets, data, stride, 0)
    return header + struct.pack(">" + "H" * cols, *range(0, stride, 2)) + \
        bytes([22 if text else 18] * cols) + payload + strings


def assemble(items, locals_count=0):
    result = bytearray([locals_count, 0, 0])
    labels, jumps = {}, []
    for item in items:
        if isinstance(item, str):
            labels[item] = len(result)
            continue
        op, *args = item
        result.append(op)
        if op == 5:
            result.extend(args[0].encode() + b"\0")
        elif op in (2, 3):
            result.extend(struct.pack(">H", args[0] & 65535))
        elif op == 4:
            result.extend(struct.pack(">i", args[0]))
        elif op in (19, 20, 67, 68, 69):
            jumps.append((len(result), args[0]))
            result.extend(b"\0\0")
        else:
            result.extend(args)
    for offset, label in jumps:
        struct.pack_into(">h", result, offset, labels[label] - offset)
    return bytes(result)


def section(variables, functions, objects=(), screen=False):
    code = bytearray()
    offset = len(functions) * 4
    for f in functions:
        code.extend(struct.pack(">I", offset))
        offset += len(f)
    code.extend(b"".join(functions))
    types, values, arrays = bytearray(), bytearray(), bytearray()
    for value in variables:
        types.append(28 if isinstance(value, list) else 21)
        values.extend(struct.pack(">i", 0 if isinstance(value, list) else value))
        if isinstance(value, list):
            arrays.extend(struct.pack(">3H", len(value), 0, len(value)))
            arrays.extend(struct.pack(">" + "i" * len(value), *value))
    root = bytes([1 if screen else 15]) + struct.pack(">2H", 1 if screen else 32769, 0)
    root += bytes(35 if screen else 1) + b"\0"
    return (root + struct.pack(">4H", 1, len(objects), len(functions), len(variables))
            + types + values + b"\0\0" + arrays + struct.pack(">I", len(code))
            + code + b"".join(objects))


def fixture_jar(path, altered_title_binding=False, unknown_pure_opcode=False,
                altered_playback_binding=False, repeated_state_users=False,
                semantic_variant=False, counts_values=(4, 3, 3), disc_number=2,
                playlist_numbers=None, contradictory_playback=False):
    capacity = max(counts_values)
    region_offset = capacity * 2
    playlist_numbers = playlist_numbers or list(range(90000, 90000 + capacity))
    def emit(items, locals_count=0):
        if semantic_variant:
            rewritten = []
            for item in items:
                if not isinstance(item, str):
                    if item[0] in (1, 2, 3):
                        item = (4, item[1])
                    rewritten.append((0,))
                rewritten.append(item)
            items = rewritten
        return assemble(items, locals_count)
    # IDs deliberately differ from the development discs.
    flag, disc, disc_count, counts, region_a, region_b = range(0xc001, 0xc007)
    selected, states, region_var, episode_var = 0x4001, 0x4002, 0x4003, 0x4004
    store = emit([(1, 0), (1, 0), (2, states), (40,),
                      (5, "storeEpisodePlaylists()"),
                      (2, 11), (3, 0xc001), (22, 4), (63,)])
    region = []
    for letter, value in (("A", region_a), ("B", region_b)):
        region += [(3, value), (44,), (4, 65537), (83,), (2, region_var), (46,),
                   (5, f"VideoSequencer.epRegionOffset = EpID.REG_{letter}")]
    region += [(63,)]
    translate = [
        (1, 0), (3, counts), (36,), (34, 5), (3, flag), (44,), (76,),
        (69, "loop"), (32, 6), (54,), (20, "end"),
        "loop", (32, 3), (3, disc_count), (44,), (15,), (69, "bad"),
        (32, 5), (1, 0), (13,), (69, "bad"),
        (32, 3), (3, counts), (36,), (34, 5),
        (32, 2), (32, 5), (6,), (34, 2),
        (32, 6), (32, 1), (14,), (69, "next"),
        (32, 6), (32, 2), (15,), (69, "next"),
        (32, 6), (32, 1), (10,), (54,), (20, "end"),
        "next", (32, 1), (32, 5), (6,), (34, 1), (65, 3, 1), (20, "loop"),
        "bad", (2, -1), (54,), "end", (63,),
    ]
    if unknown_pure_opcode:
        translate.insert(0, (21, 1, 0))
    offset = [
        (3, flag), (44,), (76,), (69, "loop"), (1, 0), (54,), (20, "end"),
        "loop", (32, 2), (3, disc), (44,), (1, 1), (10,), (15,), (69, "done"),
        (32, 1), (32, 2), (3, counts), (36,), (6,), (34, 1),
        (65, 2, 1), (20, "loop"), "done", (32, 1), (54,), "end", (63,),
    ]
    play = emit([(32, 3), (2, 0x4002), (22, 1),
                     (52,), (34, 1), (5, "playEpisode(epID = "),
                     (32, 1), (2, region_var), (44,), (6,), (32, 2), (6,),
                     (2, episode_var), (46,)] +
                ([(1, 9), (2, episode_var), (46,)] if contradictory_playback else []) +
                [(63,)], 2)
    caller = emit([(1, 0), (1, 0), (2, selected), (44,), (2, 0x4001), (22, 3), (63,)])
    enter = emit([(2, selected if not altered_title_binding else 0x4003), (44,),
                      (1, 1), (6,), (2, 10), (23, 1, 2), (63,)])
    render = emit([(5, "VideoSequencer.playEpisode()"),
                       (2, region_var if altered_playback_binding else episode_var),
                       (44,), (1, 0), (2, states), (40,), (63,)])
    if semantic_variant:
        # Same relationships with reordered addition, temporaries and different widths.
        play = emit([(32, 3), (2, 0x4002), (22, 1), (52,), (34, 1),
                     (32, 2), (2, region_var), (44,), (6,), (32, 1), (6,),
                     (1, 0), (6,), (2, episode_var), (46,), (63,)], 2)
        caller = emit([(2, selected), (44,), (34, 1),
                       (1, 0), (1, 0), (32, 1), (2, 0x4001), (22, 3), (63,)], 1)
        enter = emit([(1, 1), (2, selected), (44,), (6,), (34, 1),
                      (1, 0), (32, 1), (2, 10), (23, 1, 2), (63,)], 1)
        render = emit([(2, episode_var), (44,), (34, 1),
                       (32, 1), (1, 0), (2, states), (40,), (63,)], 1)
    parent = bytes([3]) + struct.pack(">2H", 9, 1) + bytes(7) + b"\1" + struct.pack(">3H", 31, 0, 0x4005)
    title_obj = bytes([8]) + struct.pack(">2H", 10, 9) + b"\0\0\2\0"
    state_obj = bytes([8]) + struct.pack(">2H", 11, 1) + b"\2\xff\xff\0\3\0"
    global_functions = [store, emit(region)]
    screen_functions = [play, emit(translate, 5), caller, emit(offset, 2), enter, render]
    if repeated_state_users:
        global_functions.append(store)
        screen_functions.append(render)
    global_section = section([1, disc_number, len(counts_values), list(counts_values), 0, region_offset], global_functions)
    screen_section = section([0, list(range(region_offset * 2)), 0, 0],
                             screen_functions,
                             [parent, title_obj, state_obj], screen=True)
    qco = b"QCOF" + struct.pack(">2I", 5, 13 + len(global_section)) + b"\x0d" + global_section + screen_section
    entries = [
        (3, "screen", "file:///jar/program.QCO"),
        (5, "labels", "file:///jar/labels.qct"),
        (4, "sequence", "file:///jar/sequence.qcs"),
    ] + [(64, f"opaque_{i}", f"bd://PLAYLIST:{pid:05}.ITEM:0.V1:1")
         for i, pid in enumerate(playlist_numbers)]
    payload, index = bytearray(), bytearray()
    for kind, name, uri in entries:
        index.extend(struct.pack(">I", 32 + 4 * len(entries) + len(payload) - 8))
        payload.extend(bytes([kind, 0, 0]) + name.encode() + b"\0" + uri.encode() + b"\0")
        if kind == 64:
            payload.extend(bytes(14))
    qcd = bytearray(32)
    qcd[:8] = b"QCDF" + struct.pack(">I", 4)
    struct.pack_into(">I", qcd, 16, 32)
    struct.pack_into(">H", qcd, 26, len(entries))
    qcd += index + payload
    titles = [["TITLE", "EPISODES"]] + [[f"BUTTON_{i:02}", f"TITLE {i+1}"] for i in range(sum(counts_values))]
    states_data = [[4 + i] for i in range(capacity)] + [[0]] * capacity
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("eng/RESDIR.QCD", qcd)
        archive.writestr("common/program.QCO", qco)
        archive.writestr("eng/labels.qct", table_blob(titles, True))
        archive.writestr("common/sequence.qcs", table_blob(states_data * 2))


class NavigationTest(unittest.TestCase):
    def test_order_comes_from_menu_for_different_counts_and_scrambled_playlist_ids(self):
        for counts in ((2, 5, 1), (1, 2), (6, 1, 2, 3)):
            with self.subTest(counts=counts), tempfile.TemporaryDirectory() as directory:
                jar = Path(directory) / "menu.jar"
                numbers = [80123, 7, 67234, 123, 90876, 900][:max(counts)]
                fixture_jar(jar, counts_values=counts, disc_number=2,
                            playlist_numbers=numbers, semantic_variant=True)
                result = onq.read_navigation(jar)
                self.assertEqual(result["counts"], list(counts))
                self.assertEqual([e["episode"] for e in result["episodes"]],
                                 list(range(counts[0] + 1, counts[0] + counts[1] + 1)))
                self.assertEqual([e["variants"]["offset_0"] for e in result["episodes"]],
                                 [f"bd://PLAYLIST:{n:05}.ITEM:0.V1:1" for n in numbers[:counts[1]]])

    def test_multiple_functions_using_one_state_table_are_not_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "menu.jar"
            fixture_jar(jar, repeated_state_users=True)
            result = onq.read_navigation(jar)
            self.assertEqual([e["episode"] for e in result["episodes"]], [5, 6, 7])

    def test_synthetic_program_resolves_disc_two_without_episode_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "menu.jar"
            fixture_jar(jar)
            result = onq.read_navigation(jar)
            self.assertEqual(result["counts"], [4, 3, 3])
            self.assertEqual(result["disc"], 2)
            self.assertEqual([e["episode"] for e in result["episodes"]], [5, 6, 7])
            self.assertEqual(result["episodes"][0]["title"], "TITLE 5")
            self.assertEqual(result["episodes"][0]["variants"]["offset_0"], "bd://PLAYLIST:90000.ITEM:0.V1:1")

    def test_renamed_diagnostics_and_button_keys_do_not_determine_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "menu.jar"
            fixture_jar(jar)
            with zipfile.ZipFile(jar) as archive:
                contents = {name: archive.read(name) for name in archive.namelist()}
            for name, data in contents.items():
                # Same byte lengths so the fixture's offsets remain valid.
                for marker in (b"playEpisode(epID = ", b"storeEpisodePlaylists()",
                               b"VideoSequencer.playEpisode()",
                               b"VideoSequencer.epRegionOffset = EpID.REG_A",
                               b"VideoSequencer.epRegionOffset = EpID.REG_B"):
                    data = data.replace(marker, b"X" * len(marker))
                data = data.replace(b"BUTTON_", b"Opaque_")
                contents[name] = data
            with zipfile.ZipFile(jar, "w") as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            result = onq.read_navigation(jar)
            self.assertEqual([e["episode"] for e in result["episodes"]], [5, 6, 7])

    def test_changed_title_binding_and_side_effecting_expression_fail_closed(self):
        for change in ("altered_title_binding", "unknown_pure_opcode", "altered_playback_binding",
                       "contradictory_playback"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                jar = Path(directory) / "menu.jar"
                fixture_jar(jar, **{change: True})
                with self.assertRaises(core.NeedsReview):
                    core.read_navigation(jar)

    def test_no_fallback_to_episode_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "menu.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("EP01.mpls", b"")
            with self.assertRaises(core.NeedsReview):
                core.read_navigation(jar)

    def test_truncation_and_large_dimensions_are_rejected(self):
        valid = table_blob([["TITLE", "A"], ["BUTTON_00", "B"]], True)
        for data in (valid[:12], valid[:-2], valid[:8] + struct.pack(">I", 1000000) + valid[12:]):
            with self.assertRaises((ValueError, IndexError, struct.error)):
                binary.table(data)


if __name__ == "__main__":
    unittest.main()
