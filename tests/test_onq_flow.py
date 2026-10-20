import importlib
import unittest

from test_onq import assemble, binary, onq

flow = importlib.import_module("core_test_package.onq_flow")


def trace(items):
    data = assemble(items)
    return flow.analyze(binary.instructions(data, 0, len(data)))


class FlowTest(unittest.TestCase):
    def test_aliases_and_constant_widths_preserve_array_reference(self):
        events = trace([(4, 0x4002), (34, 1), (1, 7), (1, 0),
                        (32, 1), (40,), (63,)])
        self.assertIn(("array2", 0x4002, flow.const(7), flow.const(0)),
                      [e[3] for e in events if e[0] == "array"])

    def test_branch_disagreement_is_preserved(self):
        events = trace([(2, 0x4001), (44,), (69, "else"),
                        (1, 3), (34, 1), (20, "join"),
                        "else", (1, 9), (34, 1),
                        "join", (32, 1), (2, 0x4002), (46,), (63,)])
        value = next(e[3] for e in events if e[0] == "write")
        self.assertEqual(value, ("choice", flow.const(3), flow.const(9)))
        good = ("add", ("call", 7, ("local", 3)), ("global", 8), ("local", 2))
        self.assertIsNone(onq.playback_dependencies(("choice", good, flow.const(9))))

    def test_unreachable_writes_do_not_create_evidence(self):
        events = trace([(20, "end"), (1, 9), (2, 0x4002), (46,), "end", (63,)])
        self.assertFalse(any(e[0] == "write" for e in events))

    def test_unknown_instruction_invalidates_local_origin(self):
        events = trace([(1, 7), (34, 1), (64,), (32, 1), (2, 0x4002), (46,), (63,)])
        self.assertEqual(next(e[3] for e in events if e[0] == "write"), flow.UNKNOWN)

    def test_unresolved_branch_target_is_not_ignored(self):
        self.assertEqual(flow.analyze([[0, "jump", 999], [3, "return"]]), [])

    def test_unexplained_constant_cannot_shift_all_episodes(self):
        shifted = ("add", ("call", 7, ("local", 3)), ("global", 8), flow.const(9))
        self.assertIsNone(onq.playback_dependencies(shifted))

    def test_nested_loop_expression_is_bounded(self):
        value = flow.const(1)
        for _ in range(40):
            value = ("not", value)
        self.assertEqual(flow.bounded(value), flow.UNKNOWN)

    def test_loop_does_not_fabricate_a_constant(self):
        events = trace([(1, 0), (34, 1), "loop", (65, 1, 1),
                        (2, 0x4001), (44,), (68, "loop"),
                        (32, 1), (2, 0x4002), (46,), (63,)])
        values = [e[3] for e in events if e[0] == "write"]
        self.assertTrue(not values or values == [flow.UNKNOWN])


if __name__ == "__main__":
    unittest.main()
