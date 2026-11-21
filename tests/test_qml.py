# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
from pathlib import Path
import stat
import struct
from unittest.mock import patch
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emberbsd_sdk.qml import QmlError, compile_scene, compile_scene_with_header
from emberbsd_sdk.qml_image import BitmapAssets


class QmlCompilerTest(unittest.TestCase):
    def test_root_signal_calls(self):
        def compile_signals(fields, body):
            return compile_scene("import QtQuick\nRectangle { id: lab; width: 20; height: 20; "
                                 "MouseArea { id: button; anchors.fill: parent; onClicked: "
                                 + body + " }; " + fields + " }")

        output = compile_signals("signal refreshRequested(); signal resetRequested()",
                                 '{ lab.refreshRequested(); if (lab.visible) lab.resetRequested() }')
        self.assertIn('eqml_ui_emit(ui, "refreshRequested");', output)
        self.assertIn('eqml_ui_emit(ui, "resetRequested");', output)
        self.assertNotIn("eqml_ui_log", output)
        self.assertNotIn("eqml_ui_emit", compile_signals("signal refreshRequested()",
                                                        'console.log("refreshRequested")'))
        for fields, body in (
            ("signal refreshRequested(int count)", "lab.refreshRequested()"),
            ("signal refreshRequested(); signal refreshRequested()", "lab.refreshRequested()"),
            ("signal refreshRequested()", "lab.refreshRequested(1)"),
            ("signal refreshRequested()", "lab.refreshRequest()"),
            ("signal refreshRequested()", "button.refreshRequested()"),
            ("signal refreshRequested()", "refreshRequested()"),
            ("signal refreshRequested()", "{ if (lab.refreshRequested()) lab.refreshRequested() }"),
            ("signal refreshRequested()", "(lab) => lab.refreshRequested()"),
            ("signal button()", "lab.button()"),
            ("signal if()", "lab.if()"),
            ("signal widthChanged()", "lab.widthChanged()"),
            ("signal mapToItem()", "lab.mapToItem()"),
            ("signal levelChanged(); property int level: 1", "lab.levelChanged()"),
            ("property int level: 1; signal level()", "lab.level()"),
            ("Item { signal refreshRequested() }", "lab.refreshRequested()"),
        ):
            with self.subTest(fields=fields, body=body), self.assertRaises(QmlError):
                compile_signals(fields, body)
        with self.assertRaisesRegex(QmlError, "root id"):
            compile_scene("import QtQuick\nItem { width: 1; height: 1; signal go() }")

    def test_image_decoding_and_placement(self):
        header = bytes.fromhex("89504e470d0a1a0a 0000000d 49484452") + struct.pack(
            ">IIBBBBBI", 2, 1, 8, 6, 0, 0, 0, 0)
        pam = b"P7\nWIDTH 2\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
        content = 'Image { x: 2; source: "test.png" }'
        scene = 'import QtQuick\nItem { width: 6; height: 3; %s }'
        with tempfile.TemporaryDirectory() as tmp, patch("emberbsd_sdk.qml_image.subprocess.run") as run:
            folder = Path(tmp) / "scene"
            folder.mkdir()
            (folder / "test.png").write_bytes(header)
            def compile_items(items):
                return compile_scene(scene % items, assets=BitmapAssets(folder, "/native/pngtopam"))
            run.return_value.returncode = 0
            run.return_value.stdout = pam + bytes.fromhex("ff000040 00ff00ff")
            output = compile_items(content + "; " + content)
            run.assert_called_once()
            self.assertEqual(output.count(".bitmap = &bitmap_0"), 2)
            self.assertIn("0xf800, 0x07e0", output)
            self.assertIn("0x40, 0xff", output)
            self.assertIn(".kind = EQML_IMAGE, .data.image = { .x = 2", output)
            run.return_value.stdout = pam + bytes.fromhex("ff0000ff 00ff00ff")
            output = compile_items(content + "; MouseArea { anchors.fill: parent }")
            self.assertIn("EQML_NODE_IMAGE", output)
            self.assertNotIn("bitmap_0_alpha", output)
            for field, problem in [('width: 1;', "dimensions"), ('anchors.centerIn: parent;', "explicit")]:
                with self.assertRaisesRegex(QmlError, problem):
                    compile_items(content.replace("x: 2;", field))
            for name in ("/test.png", "../escape.png", "https://remote/a.png", "test.jpeg"):
                with self.assertRaisesRegex(QmlError, "local relative PNG"):
                    compile_items(content.replace("test.png", name))
            external = Path(tmp) / "outside.png"
            external.write_bytes(header)
            (folder / "link.png").symlink_to(external)
            with self.assertRaisesRegex(QmlError, "within the QML directory"):
                compile_items(content.replace("test.png", "link.png"))
            run.return_value.stdout = pam
            with self.assertRaisesRegex(QmlError, "unexpected RGB_ALPHA"):
                compile_items(content)
            (folder / "test.png").write_bytes(header[:16] + struct.pack(">II", 4096, 4096) + header[24:])
            with self.assertRaisesRegex(QmlError, "scene budget"):
                compile_items(content)

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
        self.assertIn("#if EQML_SCENE_ABI != 5", result)
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
            ('Image {}', 'Image requires a local PNG source'),
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

    def test_header_matches_compiled_node_order_and_text_slots(self):
        source = """import QtQuick
Item { id: page; width: 32; height: 32
    Rectangle { id: cursor; z: 2; Behavior on x { NumberAnimation { duration: 321 } } }
    MouseArea { id: controls; anchors.fill: parent; property string reserved }
    Text { id: fixed; text: "literal" }
    Text { id: value }
}"""
        scene, header = compile_scene_with_header(source, "myScene")
        self.assertEqual(scene, compile_scene(source, "myScene"))
        for name, value in [("node_page", 0), ("node_controls", 1), ("node_fixed", 2),
                            ("node_value", 3), ("text_value", 1), ("node_cursor", 4),
                            ("duration_x_cursor", 321)]:
            self.assertIn(f"eqml_myScene_{name} = {value},", header)
        self.assertNotIn("text_fixed", header)
        self.assertNotIn("duration_y_cursor", header)
        self.assertNotIn("eqml_myScene_node_", scene)
        other = compile_scene_with_header(source, "myscene")[1]
        self.assertNotEqual(header.splitlines()[1], other.splitlines()[1])
        static = compile_scene_with_header("import QtQuick; Item { width: 2; height: 1 }")[1]
        self.assertIn("extern const struct eqml_scene eqml_app;", static)
        self.assertNotIn("enum {", static)

    def test_cli_header_independent_updates_and_aliases(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            source, output, header = (directory / name for name in ("App.qml", "app.c", "app.h"))
            content = "import QtQuick; Item { width: 2; height: 1; MouseArea { id: area } }"
            source.write_text(content)
            def run(dest, interface):
                return subprocess.run([sys.executable, str(ROOT / "scripts/qml-compile.py"), str(source),
                                       "-o", str(dest), "--header", str(interface)], capture_output=True)
            def stamp(path):
                info = path.stat()
                return (info.st_ino, info.st_mtime_ns)
            self.assertEqual(run(output, header).returncode, 0)
            header.chmod(0o600)
            before = stamp(output), stamp(header)
            self.assertEqual(run(output, header).returncode, 0)
            self.assertEqual((stamp(output), stamp(header)), before)
            source.write_text(content.replace("width: 2", "width: 3"))
            self.assertEqual(run(output, header).returncode, 0)
            self.assertNotEqual(stamp(output), before[0])
            self.assertEqual(stamp(header), before[1])
            changed_scene = stamp(output)
            source.write_text(content.replace("width: 2", "width: 3").replace("id: area", "id: button"))
            self.assertEqual(run(output, header).returncode, 0)
            self.assertEqual(stamp(output), changed_scene)
            self.assertNotEqual(stamp(header), before[1])
            self.assertEqual(stat.S_IMODE(header.stat().st_mode), 0o600)
            saved = output.read_bytes(), header.read_bytes(), source.read_bytes()
            alias = directory / "hardlink"
            alias.hardlink_to(source)
            symlink = directory / "symlink"
            symlink.symlink_to(output)
            for dest, interface in [(source, header), (output, source), (output, output),
                                    (output, alias), (output, symlink)]:
                self.assertNotEqual(run(dest, interface).returncode, 0)
                self.assertEqual((output.read_bytes(), header.read_bytes(), source.read_bytes()), saved)
            source.write_text(content.replace("width: 2", "width: 4"))
            for invalid in (directory, directory / "missing" / "header.h"):
                self.assertNotEqual(run(output, invalid).returncode, 0)
                self.assertEqual(output.read_bytes(), saved[0])

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
