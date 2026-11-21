# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emberbsd_sdk.qml_script import (QmlScriptError, compile_expression, compile_handler,
                                     emit_dispatch, signal_enum)


class QmlScriptTest(unittest.TestCase):
    def test_typed_handler_lowers_events_strings_and_map(self):
        source = '''(mouse) => {
            if (mouse.button == Qt.LeftButton) {
                const p = square.mapToItem(root, mouse.x, mouse.y)
                label.text = "at " + p.x + "," + p.y
            } else {
                label.text = "other"
            }
        }'''
        result = compile_handler(
            source, "on_pressed", current_node=4,
            nodes={"root": 0, "square": 3},
            slots={"label.text": {"index": 2, "type": "string"}})
        self.assertIn("struct eqml_ui *ui", result)
        self.assertIn("eqml_ui_map(ui, 3, 0, mouse->x, mouse->y)", result)
        self.assertIn("eqml_ui_set(ui, 2", result)
        self.assertIn("EQML_BUTTON_LEFT", result)
        self.assertEqual(result.count("snprintf"), 1)
        self.assertIn("%s%d%s%d", result)

    def test_dispatch_is_keyed_by_node_and_signal(self):
        result = emit_dispatch("scene_handler", {(4, "onEntered"): "on_enter"})
        self.assertIn("case 4", result)
        self.assertIn("EQML_ENTERED", result)
        self.assertIn("on_enter(ui, node, signal, mouse)", result)

    def test_local_slot_alias_and_pressed_state_lower_generically(self):
        result = compile_handler(
            "{ buttonID = qsTr('LeftButton'); label.text = buttonID + (area.containsPress ? '!' : '') }",
            "on_pressed", current_node=3, nodes={"area": 3},
            slots={"buttonID": {"index": 1, "type": "string"},
                   "label.text": {"index": 2, "type": "string"}})
        self.assertIn("eqml_ui_set(ui, 1", result)
        self.assertIn("eqml_ui_contains_press(ui, 3)", result)
        self.assertIn("eqml_ui_string(ui, 1)", result)

    def test_pressed_and_contains_press_are_distinct_properties(self):
        result = compile_handler(
            "label.text = pressed ? 'pressed' : (area.containsPress ? 'inside' : 'outside')",
            "on_exited", current_node=3, nodes={"area": 3},
            slots={"label.text": {"index": 2, "type": "string"}})
        self.assertIn("eqml_ui_pressed(ui, 3)", result)
        self.assertIn("eqml_ui_contains_press(ui, 3)", result)

    def test_integer_relational_precedence(self):
        result = compile_expression("1 + 2 < 4 && -5 >= -6")
        self.assertEqual(result.type, "bool")
        for left, operator in [("true", ">"), ("'abc'", "<=")]:
            with self.assertRaisesRegex(QmlScriptError, "typed operation"):
                compile_expression(f"{left} {operator} {left}")

    def test_shared_typed_expression_lowering(self):
        result = compile_expression("box.width - width - 10",
                                    geometry={"box.width": 320},
                                    locals_={"width": {"type": "int", "code": "120"}})
        self.assertEqual((result.type, result.code), ("int", "((((320) - (120))) - (10))"))
        opacity = compile_expression("area.containsPress ? 0.6 : 1.0",
                                     nodes={"area": 4}, geometry={})
        self.assertEqual(opacity.type, "float")
        self.assertIn("eqml_ui_contains_press(ui, 4)", opacity.code)

    def test_signals_are_canonical_and_unknown_signals_rejected(self):
        self.assertEqual(signal_enum("onPressAndHold"), "EQML_PRESS_AND_HOLD")
        with self.assertRaisesRegex(QmlScriptError, "unsupported signal"):
            signal_enum("onDestroyed")

    def test_readonly_and_scoped_locals(self):
        for source, message in [
                ("{ const a = 1; a = 2 }", "read-only"),
                ("{ { let x = 1 }; label.text = '' + x }", "unknown identifier")]:
            with self.subTest(source=source), self.assertRaisesRegex(QmlScriptError, message):
                compile_handler(source, "handle", current_node=0, nodes={},
                                slots={"label.text": {"index": 0, "type": "string"}})

    def test_strings_have_value_semantics_and_math_is_checked(self):
        code = compile_handler("""(m) => {
            let old = label.text
            { old = old + m.x }
            label.text = old + (m.x + 1)
        }""", "handle", current_node=0, nodes={},
                               slots={"label.text": {"index": 0, "type": "string"}})
        self.assertEqual(code.count("eqml_string_copy("), 2)
        self.assertIn("errno = ERANGE", code)
        with self.assertRaisesRegex(QmlScriptError, "no mouse event"):
            compile_handler("(m) => console.log(m.x)", "changed", current_node=0,
                            nodes={}, slots={}, allow_mouse=False)

    def test_unsupported_dynamic_js_is_rejected(self):
        with self.assertRaisesRegex(QmlScriptError, "unsupported function call"):
            compile_handler("eval('x')", "bad", current_node=0,
                            nodes={}, slots={})


if __name__ == "__main__":
    unittest.main()
