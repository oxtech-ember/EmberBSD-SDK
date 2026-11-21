# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emberbsd_sdk.qml import QmlError, compile_scene


class QmlCompilerTest(unittest.TestCase):
    def test_item_visibility_and_opacity(self):
        source = """import QtQuick
Item { width: 320; height: 480
    Item { id: group; x: 20; y: 30; width: 100; height: 60
        MouseArea { anchors.fill: parent; onClicked: top.visible = !group.visible }
        Rectangle { id: top; opacity: 0.5; visible: false }
    }
}"""
        result = compile_scene(source, "overlay")
        self.assertEqual(result.count("EQML_NODE_ITEM"), 2)
        self.assertIn("eqml_ui_set_visible(ui, 3, (!(eqml_ui_visible(ui, 1))))", result)
        self.assertIn(".transparency = 128, .hidden = 1", result)
        self.assertIn(".background = 0xffff", result)
        for before, after, error in [
            ("opacity: 0.5", "opacity: 2", "between zero and one"),
            ("visible: false", "visible: group.visible", "boolean literal"),
            ("!group.visible", "4", "requires a boolean"),
        ]:
            with self.subTest(after=after), self.assertRaisesRegex(QmlError, error):
                compile_scene(source.replace(before, after))

    def test_visible_property_cannot_be_redeclared(self):
        for declaration in ("property int visible: 1", "property bool visible: true",
                            "MouseArea { property string visible }"):
            source = f"import QtQuick\nItem {{ width: 4; height: 4; {declaration} }}"
            with self.subTest(declaration=declaration), self.assertRaisesRegex(QmlError, "built-in"):
                compile_scene(source)
        self.assertIn("eqml_app", compile_scene("import QtQuick\nItem { id: visible; width: 4; height: 4 }"))

    def test_behavior_and_coordinate_updates_compile_to_runtime_api(self):
        source = '''import QtQuick 2.0
Rectangle { width: 400; height: 400
    Rectangle { id: rect; width: 100; height: 100; y: 100; color: "red"
        Behavior on x { NumberAnimation { duration: 1000 } }
        MouseArea { anchors.fill: parent; onClicked: rect.x += 50 }
    }
}'''
        result = compile_scene(source, "behavior")
        self.assertIn("eqml_ui_coordinate(ui, 1, EQML_AXIS_X)", result)
        self.assertIn("eqml_ui_set_coordinate(ui, 1, EQML_AXIS_X", result)
        self.assertIn(", 1000) != 0", result)
        self.assertIn("int64_t _eqml_wide_", result)

    def test_coordinate_assignment_is_immediate_without_behavior(self):
        source = '''import QtQuick
Rectangle { width: 40; height: 40
    Rectangle { id: rect; width: 10; height: 10
        MouseArea { anchors.fill: parent; onClicked: rect.y = rect.y + 3 }
    }
}'''
        result = compile_scene(source)
        self.assertIn("eqml_ui_coordinate(ui, 1, EQML_AXIS_Y)", result)
        self.assertIn("eqml_ui_set_coordinate(ui, 1, EQML_AXIS_Y", result)
        self.assertIn(", 0) != 0", result)

    def test_rejects_coordinate_writes_on_anchored_axes(self):
        source = '''import QtQuick
Rectangle { width: 40; height: 40
    Rectangle { id: rect; width: 10; height: 10; anchors.horizontalCenter: parent.horizontalCenter
        MouseArea { anchors.fill: parent; onClicked: rect.x = 4 }
    }
}'''
        with self.assertRaisesRegex(QmlError, "x is controlled by anchors"):
            compile_scene(source)

    def test_behavior_rejects_unsupported_target_or_anchored_axis(self):
        cases = [
            ('''Rectangle { width: 20; height: 20
                Rectangle { id: box; width: 5; height: 5
                    anchors.horizontalCenter: parent.horizontalCenter
                    Behavior on x { NumberAnimation { duration: 200 } }
                }
                MouseArea {}
            }''', "Behavior on x conflicts with anchors"),
            ('''Rectangle { width: 20; height: 20
                Rectangle { id: box; width: 5; height: 5
                    Behavior on width { NumberAnimation { duration: 200 } }
                }
            }''', "Behavior supports only x or y"),
            ('''Rectangle { width: 20; height: 20
                Rectangle { id: box; width: 5; height: 5
                    Behavior on x { NumberAnimation { duration: 200; loops: 2 } }
                }
                MouseArea {}
            }''', "NumberAnimation supports only one duration"),
            ('''Rectangle { width: 20; height: 20
                Rectangle { id: box; width: 5; height: 5
                    Behavior on x { NumberAnimation { duration: 2147483648 } }
                }
                MouseArea {}
            }''', "integer outside 0..2147483647"),
        ]
        for source, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(QmlError, reason):
                compile_scene("import QtQuick 2.0\n" + source)

    def test_local_handler_name_cannot_shadow_qml_object_id(self):
        source = '''import QtQuick
Rectangle { width: 20; height: 20
    Rectangle { id: rect; width: 5; height: 5
        MouseArea { anchors.fill: parent
            onClicked: { let rect = 0; rect.x += 50 }
        }
    }
}'''
        with self.assertRaisesRegex(QmlError, "local variable shadows QML object id rect"):
            compile_scene(source)

    def test_mousearea_scene_compiles_to_interactive_program(self):
        source = '''import QtQuick
Rectangle { id: root; width: 320; height: 480
    Rectangle { id: square; width: 120; height: 120
        anchors { top: parent.top; left: parent.left; margins: 10 }
        color: "red"; opacity: area.containsPress ? 0.6 : 1.0
        Text { text: qsTr('Tap'); font.pixelSize: 16; anchors.centerIn: parent }
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true
            property string buttonID
            acceptedButtons: Qt.AllButtons
            onPressed: (mouse) => {
                if (mouse.button == Qt.LeftButton) buttonID = qsTr('LeftButton')
                label . text = "Pressed: " + buttonID
                const p = square.mapToItem(root, mouse.x, mouse.y)
                coords.text = + p.x + "," + p.y
            }
            onReleased: (mouse) => { label.text = "Released " + mouse.isClick }
            onDoubleClicked: label.text = "Double clicked"
        }
    }
    Rectangle { id: dragItem; width: 120; height: 120
        x: root.width - width - 10; y: 10
        MouseArea { anchors.fill: parent; drag.target: dragItem
            drag.axis: Drag.XAndYAxis; drag.minimumX: 0
            drag.maximumX: root.width - parent.width
            drag.minimumY: 0; drag.maximumY: root.height - parent.width
        }
    }
    Text { id: label; anchors { bottom: coords.top; horizontalCenter: parent.horizontalCenter; margins: 20 }
        onTextChanged: console.log(text)
    }
    Text { id: coords; anchors { bottom: parent.bottom; horizontalCenter: parent.horizontalCenter; margins: 20 } }
}'''
        result = compile_scene(source, "mousearea")
        self.assertIn('#include "eqml_input.h"', result)
        self.assertIn("#if EQML_SCENE_ABI != 4", result)
        self.assertIn(".program = &program", result)
        self.assertIn("static const struct eqml_node nodes[]", result)
        self.assertEqual(result.count(".kind = EQML_NODE_AREA"), 2)
        self.assertIn("EQML_EDGE_TOP", result)
        self.assertIn("EQML_EDGE_BOTTOM", result)
        self.assertIn("EQML_BUTTON_ALL", result)
        self.assertIn(".drag_target = 4", result)
        self.assertIn(".opacity_area = 3", result)
        self.assertIn("EQML_TEXT_CHANGED", result)
        self.assertIn("eqml_ui_map(ui, 1, 0", result)

    def test_interactive_z_sibling_order_and_default_drag_axes(self):
        source = '''import QtQuick
Rectangle { width: 40; height: 40
    Rectangle { id: low; width: 20; height: 20; z: 0; color: "red" }
    Rectangle { id: high; width: 20; height: 20; z: 4; color: "blue"
        MouseArea { anchors.fill: parent; drag.target: high
            drag.minimumX: 0; drag.maximumX: 20
            drag.minimumY: 0; drag.maximumY: 20
        }
    }
}'''
        result = compile_scene(source)
        self.assertLess(result.index(".color = 0xf800"), result.index(".color = 0x001f"))
        self.assertIn(".drag_axes = 3", result)

    def test_interactive_rejects_invalid_z_drag_and_layout(self):
        cases = [
            ('Rectangle { width: 10; height: 10; Rectangle { width: 1; height: 1; z: -1 } MouseArea {} }', 'negative z'),
            ('''Rectangle { width: 20; height: 20
                Rectangle { id: box; width: 10; height: 10
                    MouseArea { drag.target: box; drag.minimumX: 0; drag.maximumX: 5 }
                }
            }''', 'vertical drag requires minimumY'),
            ('''Rectangle { width: 20; height: 20
                Text { id: first; anchors.bottom: second.top; text: "a" }
                Text { id: second; anchors.bottom: first.top; text: "b" }
                MouseArea { }
            }''', 'layout cycle'),
            ('''Rectangle { width: 20; height: 20
                Text { id: label; anchors.bottom: label.top; text: "a" }
                MouseArea { }
            }''', 'cannot target itself'),
            ('''Rectangle { width: 20; height: 20
                MouseArea { onEntered: (mouse) => { console.log(mouse.x) } }
            }''', 'no mouse event parameter'),
            ('''Rectangle { width: 20; height: 20
                Text { onTextChanged: (mouse) => { console.log(mouse.x) } }
                MouseArea { }
            }''', 'no mouse event parameter'),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(QmlError, reason):
                compile_scene("import QtQuick\n" + body)

    def test_interactive_text_slots_follow_assignments_not_source_patterns(self):
        source = '''import QtQuick
Rectangle { width: 20; height: 20
    Text { id: label; text: "ready" }
    MouseArea { onClicked: { // label.text = fake
        console.log("label.text = also fake")
    } }
}'''
        result = compile_scene(source)
        self.assertIn('.text = "ready", .text_slot = -1', result)
        referenced = source.replace(
            'console.log("label.text = also fake")',
            'if (label.text == "ready") console.log("still ready")')
        result = compile_scene(referenced)
        self.assertIn('.text = "ready", .text_slot = 0', result)

    def test_interactive_runtime_limits_fail_during_compilation(self):
        too_many_nodes = "\n".join(
            "Rectangle { width: 0; height: 0 }" for _ in range(255)) + "\nMouseArea {}"
        with self.assertRaisesRegex(QmlError, "256 nodes"):
            compile_scene("import QtQuick\nRectangle { width: 1; height: 1\n" +
                          too_many_nodes + "\n}")
        with self.assertRaisesRegex(QmlError, "255 printable bytes"):
            compile_scene("import QtQuick\nRectangle { width: 1; height: 1\n" +
                          'Text { text: "' + "x" * 256 + '" }\nMouseArea {}\n}')
        properties = "\n".join(f"property string value{i}" for i in range(257))
        with self.assertRaisesRegex(QmlError, "256 string slots"):
            compile_scene("import QtQuick\nRectangle { width: 1; height: 1\n" +
                          "MouseArea {\n" + properties + "\n}\n}")

    def test_model_and_layout_from_source(self):
        scene = '''import QtQuick
Rectangle { id: board; width: 80; height: 32; color: "#ff0000"
    Text { anchors.centerIn: parent; text: "T=" + board.value + " C" }
    property int value: -8
    Text { x: -4; y: 2; color: "#00ff00"; text: "ok" }
}'''
        result = compile_scene(scene, "gauge")
        self.assertIn("eqml_gauge =", result)
        self.assertIn(".width = 80, .height = 32, .background = 0xf800", result)
        self.assertIn('.name = "value", .initial = -8', result)
        self.assertIn('.prefix = "T=", .suffix = " C", .binding = 0', result)
        self.assertIn("EQML_HCENTER | EQML_VCENTER", result)
        self.assertIn(".x = -4, .y = 2, .color = 0x07e0", result)
        self.assertEqual(result, compile_scene(scene, "gauge"))

    def test_comments_escaping_and_empty_scene(self):
        self.assertIn('.item_count = 0', compile_scene('import QtQuick; Rectangle { width: 1; height: 2 }'))
        result = compile_scene(r'''import QtQuick
Rectangle { /* text: fake */ width: 1; height: 2
Text { text: "\"??/" } // question marks and a quoted character
}''')
        self.assertIn(r'.prefix = "\"\?\?/"', result)

    def test_parent_coordinates_and_subtree_stacking(self):
        scene = '''import QtQuick
Rectangle { width: 20; height: 15
    Rectangle { x: -2; y: 3; width: 8; height: 4; color: "lime"
        Rectangle { x: 4; y: 5; z: -1; width: 2; height: 3; color: "green" }
        Text { anchors.centerIn: parent; text: "inner"; z: 2000 }
    }
    Rectangle { width: 1; height: 1; color: "blue" }
}'''
        result = compile_scene(scene)
        self.assertIn(".x = 2, .y = 8, .width = 2, .height = 3, .color = 0x0400", result)
        self.assertIn(".parent_width = 8, .parent_height = 4", result)
        self.assertLess(result.index("0x0400"), result.index("0x07e0"))
        self.assertLess(result.index("0x07e0"), result.index('"inner"'))
        self.assertLess(result.index('"inner"'), result.index("0x001f"))
        self.assertIn(".item_count = 4", result)

    def test_zero_size_parent_leaves_children_visible(self):
        scene = '''import QtQuick
Rectangle { width: 20; height: 15
    Rectangle { x: 7; Rectangle { x: -2; width: 5; height: 3; color: "RED" } }
}'''
        result = compile_scene(scene)
        self.assertIn(".x = 5, .y = 0, .width = 5, .height = 3, .color = 0xf800", result)
        self.assertIn(".item_count = 1", result)

    def test_grid_resolves_unequal_rows_and_columns_on_host(self):
        scene = '''import QtQuick
Rectangle { width: 30; height: 20
    Grid { columns: 2; spacing: 1; anchors.centerIn: parent
        Rectangle { width: 4; height: 3; color: "red" }
        Rectangle { width: 0; height: 9 }
        Rectangle { width: 7; height: 5; color: "blue" }
        Rectangle { width: 6; height: 2; color: "white"; z: -1 }
    }
}'''
        result = compile_scene(scene)
        for coordinates in (".x = 8, .y = 6, .width = 4", ".x = 15, .y = 6, .width = 7",
                            ".x = 8, .y = 12, .width = 6"):
            self.assertIn(coordinates, result)
        self.assertIn(".item_count = 3", result)

    def test_row_positions_unequal_children_and_spacing_on_host(self):
        scene = '''import QtQuick
Rectangle { width: 50; height: 20
    Row { spacing: 3; anchors.centerIn: parent
        Rectangle { width: 5; height: 7; color: "red" }
        Rectangle { width: 2; height: 3; color: "green" }
        Rectangle { width: 4; height: 9; color: "blue" }
    }
}'''
        result = compile_scene(scene)
        self.assertIn(".x = 16, .y = 5, .width = 5, .height = 7", result)
        self.assertIn(".x = 24, .y = 5, .width = 2, .height = 3", result)
        self.assertIn(".x = 29, .y = 5, .width = 4, .height = 9", result)
        self.assertIn(".item_count = 3", result)

    def test_row_initial_layout_direction_from_boolean_property(self):
        scene = '''import QtQuick 2.0
Item { width: 120; height: 40
    property bool testRightToLeft: false
    Row { spacing: 4; layoutDirection: testRightToLeft ? Qt.RightToLeft : Qt.LeftToRight
        Rectangle { objectName: "one"; width: 5; height: 7; color: "red" }
        Rectangle { objectName: "two"; width: 2; height: 3; color: "green" }
        Rectangle { objectName: "three"; width: 4; height: 9; color: "blue" }
    }
}'''
        ltr = compile_scene(scene)
        rtl = compile_scene(scene.replace("testRightToLeft: false", "testRightToLeft: true"))
        for x in (0, 9, 15):
            self.assertIn(f".x = {x}, .y = 0", ltr)
        for x in (14, 8, 0):
            self.assertIn(f".x = {x}, .y = 0", rtl)
        self.assertIn(".properties = NULL, .property_count = 0", ltr)
        self.assertIn(".properties = NULL, .property_count = 0", rtl)
        self.assertNotIn('"testRightToLeft"', ltr)
        with self.assertRaisesRegex(QmlError, "root boolean properties must select"):
            compile_scene("import QtQuick\nRectangle { width: 2; height: 2\nproperty bool unused: false\n}")
        later = '''import QtQuick
Item { width: 20; height: 10
    Row { spacing: 2; layoutDirection: reverse ? Qt.RightToLeft : Qt.LeftToRight
        Rectangle { width: 4; height: 2; color: "red" }
        Rectangle { width: 3; height: 4; color: "blue" }
    }
    property bool reverse: true
}'''
        self.assertIn(".x = 0, .y = 0, .width = 3", compile_scene(later))

    def test_row_omits_zero_sized_children_from_spacing(self):
        scene = '''import QtQuick
Rectangle { width: 20; height: 10
    Row { spacing: 2; anchors.centerIn: parent
        Rectangle { width: 4; height: 2; color: "red" }
        Rectangle { width: 0; height: 3; color: "green" }
        Rectangle { width: 3; height: 4; color: "blue" }
    }
}'''
        result = compile_scene(scene)
        self.assertIn(".x = 5, .y = 3, .width = 4, .height = 2", result)
        self.assertIn(".x = 11, .y = 3, .width = 3, .height = 4", result)
        self.assertIn(".item_count = 2", result)
        empty = compile_scene("import QtQuick\nRectangle { width: 20; height: 10\nRow {}\n}")
        self.assertIn(".item_count = 0", empty)

    def test_refuse_unsupported_or_ambiguous_semantics(self):
        cases = [
            ('Text { text: other.value }', 'unknown root integer'),
            ('Text { onClicked: run() }', 'unsupported Text'),
            ('Text { text: value + value }', 'single integer'),
            ('Text { anchors.centerIn: parent; x: 0 }', 'overlapping'),
            ('Text { x: 32768 }', 'outside -32768..32767'),
            ('Text { color: "#fed" }', 'opaque'),
            ('Text { font.pixelSize: 0 }', 'outside'),
            ('property int width: 3', 'invalid or duplicate'),
            ('Text { text: "é" }', 'printable ASCII'),
            ('Text { text: "' + 'x' * 4097 + '" }', '4096 bytes'),
            ('Text { x: 0 y: 0 }', 'separate fields'),
            ('Text { text: "a"; text: "b" }', 'duplicate'),
            ('Rectangle { id: part } Text { id: part }', 'duplicate object id'),
            ('property int part: 3; Rectangle { id: part }', 'id also names a root property'),
            ('Rectangle { opacity: 0 }', 'unsupported Rectangle field opacity'),
            ('Image {}', 'unsupported object or grouped property Image'),
            ('Rectangle {' * 65 + '}' * 65, 'at most 64 nested'),
            ('Grid { Text { text: "item" } }', 'Grid places fixed-size'),
            ('Grid { Rectangle { x: 1 } }', 'Grid determines'),
            ('Grid { columns: 2; spacing: 4096; Rectangle { width: 10; height: 1 } }',
             'implicit Grid size exceeds'),
            ('Row { layoutDirection: false ? Qt.RightToLeft : Qt.LeftToRight }',
             'supported static Qt direction'),
            ('Row { width: 10 }', 'unsupported Row field width'),
        ]
        for body, reason in cases:
            with self.subTest(body=body), self.assertRaisesRegex(QmlError, reason):
                compile_scene("import QtQuick\nRectangle { width: 1; height: 1\n" + body + "\n}")

    def test_reject_unsupported_qtquick_import_major(self):
        with self.assertRaisesRegex(QmlError, "only QtQuick 2.0"):
            compile_scene("import QtQuick 6.0\nRectangle { width: 1; height: 1 }")

    def test_cli_keeps_unchanged_output_and_selected_permissions(self):
        with tempfile.TemporaryDirectory() as work:
            source, output = Path(work) / "Screen.qml", Path(work) / "scene.c"
            args = [sys.executable, "-B", str(ROOT / "scripts/qml-compile.py"), str(source), "-o", str(output)]
            source.write_text("import QtQuick; Rectangle { width: 4; height: 2 }")
            subprocess.run(args, check=True, umask=0o022)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)
            modified, inode = output.stat().st_mtime_ns, output.stat().st_ino
            subprocess.run(args, check=True)
            self.assertEqual((output.stat().st_mtime_ns, output.stat().st_ino), (modified, inode))
            output.chmod(0o600)
            source.write_text("import QtQuick; Rectangle { width: 4; height: 7 }")
            subprocess.run(args, check=True, umask=0o022)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertIn(".height = 7", output.read_text())

    def test_cli_keeps_previous_output_and_input_on_error(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "Broken.qml"
            output = Path(work) / "scene.c"
            source.write_text("import QtQuick\nItem {}")
            output.write_text("previous")
            for destination in (output, source):
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/qml-compile.py"),
                                         str(source), "-o", str(destination)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertIn("Item {}", source.read_text())
            self.assertEqual(output.read_text(), "previous")


if __name__ == "__main__":
    unittest.main()
