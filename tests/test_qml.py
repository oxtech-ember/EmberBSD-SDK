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
        self.assertIn('.text_count = 0', compile_scene('import QtQuick; Rectangle { width: 1; height: 2 }'))
        result = compile_scene(r'''import QtQuick
Rectangle { /* text: fake */ width: 1; height: 2
Text { text: "\"??/" } // question marks and a quoted character
}''')
        self.assertIn(r'.prefix = "\"\?\?/"', result)

    def test_refuse_unsupported_or_ambiguous_semantics(self):
        cases = [
            ('Text { text: other.value }', 'unknown root integer'),
            ('Text { onClicked: run() }', 'unsupported Text'),
            ('Text { text: value + value }', 'single integer'),
            ('Text { anchors.centerIn: parent; x: 0 }', 'overlapping'),
            ('Text { color: "#fed" }', 'opaque'),
            ('Text { font.pixelSize: 0 }', 'outside'),
            ('property int width: 3', 'invalid or duplicate'),
            ('Text { text: "é" }', 'printable ASCII'),
            ('Text { text: "' + 'x' * 4097 + '" }', '4096 bytes'),
            ('Text { x: 0 y: 0 }', 'separate fields'),
            ('Text { text: "a"; text: "b" }', 'duplicate'),
        ]
        for body, reason in cases:
            with self.subTest(body=body), self.assertRaisesRegex(QmlError, reason):
                compile_scene("import QtQuick\nRectangle { width: 1; height: 1\n" + body + "\n}")

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
