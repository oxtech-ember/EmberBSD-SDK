# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compile the EmberBSD embedded QML profile to the eqml scene ABI."""
from dataclasses import dataclass
import json
import re


class QmlError(ValueError):
    pass


TOKEN = re.compile(
    r'(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"(?:[^"\\\n\r]|\\.)*")|(?P<int>-?(?:0|[1-9][0-9]*))'
    r'|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<punct>[{}.:;+])'
)


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    column: int

    def error(self, message):
        return QmlError(f"{self.line}:{self.column}: {message}")


@dataclass
class Item:
    kind: str
    fields: dict
    children: list


# The opaque basic SVG color keywords. Qt's "green" is distinct from "lime".
BASIC_COLORS = {
    "aqua": "#00ffff", "black": "#000000", "blue": "#0000ff", "fuchsia": "#ff00ff",
    "gray": "#808080", "green": "#008000", "lime": "#00ff00", "maroon": "#800000",
    "navy": "#000080", "olive": "#808000", "purple": "#800080", "red": "#ff0000",
    "silver": "#c0c0c0", "teal": "#008080", "white": "#ffffff", "yellow": "#ffff00",
}


def tokens(source):
    pos, line, column = 0, 1, 1
    while pos < len(source):
        match = TOKEN.match(source, pos)
        if not match:
            raise Token("", line, column).error("unexpected or unterminated token")
        value = match.group()
        if match.lastgroup not in ("space", "comment"):
            yield Token(value, line, column)
        if "\n" in value:
            line += value.count("\n")
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)
        pos = match.end()
    yield Token("", line, column)


class Parser:
    def __init__(self, source):
        self.stream = iter(tokens(source))
        self.current = next(self.stream)
        self.last = self.current
        self.ids = set()

    def take(self, expected=None):
        token = self.current
        if not token.text or (expected is not None and token.text != expected):
            raise token.error(f"expected {expected or 'a value'}, got {token.text or 'end of input'}")
        self.last = token
        self.current = next(self.stream)
        return token

    def name(self):
        token = self.take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token.text):
            raise token.error("expected an identifier")
        return token

    def path(self):
        parts = [self.name().text]
        while self.current.text == ".":
            self.take(".")
            parts.append(self.name().text)
        return ".".join(parts)

    def object_id(self):
        token = self.name()
        if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", token.text) or token.text in RESERVED:
            raise token.error("invalid object id")
        if token.text in self.ids:
            raise token.error(f"duplicate object id {token.text}")
        self.ids.add(token.text)
        return token.text

    def integer(self, low, high):
        token = self.take()
        if not re.fullmatch(r"-?(0|[1-9][0-9]{0,9})", token.text):
            raise token.error("expected a decimal integer")
        value = int(token.text)
        if not low <= value <= high:
            raise token.error(f"integer outside {low}..{high}")
        return value

    def string(self):
        token = self.take()
        try:
            value = json.loads(token.text)
        except ValueError:
            raise token.error("expected a double-quoted string") from None
        if not isinstance(value, str) or any(not 32 <= ord(c) <= 126 for c in value):
            raise token.error("expected a string of printable ASCII characters")
        return value

    def color(self):
        token = self.current
        rgb = self.string()
        rgb = BASIC_COLORS.get(rgb.lower(), rgb)
        if not re.fullmatch(r"#[a-fA-F0-9]{6}", rgb):
            raise token.error("expected an opaque #RRGGBB or basic SVG color")
        r, g, b = bytes.fromhex(rgb[1:])
        return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)

    def finish_field(self):
        if self.current.text == ";":
            self.take(";")
        elif self.current.text not in ("}", "") and self.current.line == self.last.line:
            raise self.current.error("separate fields with a newline or semicolon")

    def parent_anchor(self, key, start):
        path = self.path()
        if path != ANCHORS[key]:
            raise start.error(f"{key} requires {ANCHORS[key]}")
        return path

    def binding(self):
        # One integer reference, with optional literal text on either side.
        prefix, suffix, reference = "", "", None
        while True:
            if self.current.text.startswith('"'):
                text = self.string()
                if reference is None:
                    prefix += text
                else:
                    suffix += text
            else:
                token = self.current
                if reference is not None:
                    raise token.error("text bindings support a single integer reference")
                reference = (token, self.path())
            if self.current.text != "+":
                break
            self.take("+")
        if len(prefix) > 4096 or len(suffix) > 4096:
            raise self.last.error("each literal part of a text binding must fit in 4096 bytes")
        return prefix, reference, suffix

    def text_item(self):
        fields = {}
        self.take("{")
        while self.current.text != "}":
            start = self.current
            key = self.path()
            if key in fields:
                raise start.error(f"duplicate Text field {key}")
            self.take(":")
            if key == "text":
                value = self.binding()
            elif key == "id":
                value = self.object_id()
            elif key in ("x", "y"):
                value = self.integer(-32768, 32767)
            elif key == "z":
                value = self.integer(-(2**31), 2**31 - 1)
            elif key == "color":
                value = self.color()
            elif key == "font.pixelSize":
                value = self.integer(7, 64)
            elif key in ANCHORS:
                value = self.parent_anchor(key, start)
            else:
                raise start.error(f"unsupported Text field {key}")
            fields[key] = value
            self.finish_field()
        end = self.take("}")
        check_anchors(fields, end)
        return Item("Text", fields, [])

    def container(self, kind, depth, properties):
        if depth > 64:
            raise self.current.error("at most 64 nested containers are supported")
        self.take("{")
        fields, children = {}, []
        while self.current.text != "}":
            start = self.name()
            key = start.text
            while self.current.text == ".":
                self.take(".")
                key += "." + self.name().text
            if key in ("Text", "Rectangle", "Grid"):
                if kind == "Grid" and key != "Rectangle":
                    raise start.error("Grid places fixed-size Rectangle children")
                item = self.text_item() if key == "Text" else self.container(key, depth + 1, properties)
                if kind == "Grid" and ("x" in item.fields or "y" in item.fields):
                    raise start.error("Grid determines the x and y of its children")
                children.append(item)
                if self.current.text == ";":
                    self.take(";")
                continue
            if key == "property":
                if depth:
                    raise start.error("integer properties belong to the root Rectangle")
                self.take("int")
                token = self.name()
                key = token.text
                if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", key) or key in RESERVED or key in properties:
                    raise token.error(f"invalid or duplicate integer property {key}")
                self.take(":")
                properties[key] = self.integer(-(2**31), 2**31 - 1)
            else:
                if key in fields:
                    raise start.error(f"duplicate {kind} field {key}")
                if self.current.text == "{":
                    raise start.error(f"unsupported object or grouped property {key}")
                self.take(":")
                if key == "id":
                    fields[key] = self.object_id()
                elif kind == "Rectangle" and key in ("width", "height"):
                    fields[key] = self.integer(0 if depth else 1, 4096)
                elif kind == "Rectangle" and key == "color":
                    fields[key] = self.color()
                elif kind == "Grid" and key == "columns":
                    fields[key] = self.integer(1, 4096)
                elif kind == "Grid" and key == "spacing":
                    fields[key] = self.integer(0, 4096)
                elif kind == "Grid" and key in ANCHORS:
                    fields[key] = self.parent_anchor(key, start)
                elif depth and key in ("x", "y"):
                    fields[key] = self.integer(-32768, 32767)
                elif depth and key == "z":
                    fields[key] = self.integer(-(2**31), 2**31 - 1)
                else:
                    raise start.error(f"unsupported {kind} field {key}")
            self.finish_field()
        end = self.take("}")
        if depth == 0:
            for key in ("width", "height"):
                if key not in fields:
                    raise end.error(f"Rectangle requires {key}")
            ambiguous = self.ids.intersection(properties)
            if ambiguous:
                raise end.error(f"object id also names an integer property: {min(ambiguous)}")
        if kind == "Grid":
            check_anchors(fields, end)
            place_grid(fields, children, end)
        return Item(kind, fields, children)

    def scene(self):
        self.take("import")
        self.take("QtQuick")
        self.finish_field()
        self.take("Rectangle")
        properties = {}
        root = self.container("Rectangle", 0, properties)
        if self.current.text:
            raise self.current.error("unexpected content after the scene")
        return root, properties


ANCHORS = {"anchors.horizontalCenter": "parent.horizontalCenter",
           "anchors.verticalCenter": "parent.verticalCenter", "anchors.centerIn": "parent"}
RESERVED = {"id", "parent", "width", "height", "color", "anchors", "property", "import",
            "int", "x", "y", "z", "text", "font", "true", "false", "null", "undefined"}


def check_anchors(fields, token):
    hc = "anchors.horizontalCenter" in fields
    vc = "anchors.verticalCenter" in fields
    both = "anchors.centerIn" in fields
    if (both and (hc or vc)) or ((both or hc) and "x" in fields) or ((both or vc) and "y" in fields):
        raise token.error("overlapping anchor and position constraints")


def place_grid(fields, children, end):
    cells = [child.fields for child in children
             if child.fields.get("width", 0) and child.fields.get("height", 0)]
    if not cells:
        return
    columns = fields.get("columns", 4)
    spacing = fields.get("spacing", 0)
    # Qt includes the first trailing empty column when the first row is short.
    widths = [0] * min(columns, len(cells) + 1)
    heights = [0] * ((len(cells) + columns - 1) // columns)
    for index, cell in enumerate(cells):
        row, col = divmod(index, columns)
        widths[col] = max(widths[col], cell["width"])
        heights[row] = max(heights[row], cell["height"])
    def offsets(sizes):
        result = [0]
        for size in sizes[:-1]:
            result.append(result[-1] + size + spacing)
        return result
    xs, ys = offsets(widths), offsets(heights)
    fields["width"], fields["height"] = xs[-1] + widths[-1], ys[-1] + heights[-1]
    if max(fields["width"], fields["height"]) > 4096:
        raise end.error("implicit Grid size exceeds 4096 pixels")
    for index, cell in enumerate(cells):
        row, col = divmod(index, columns)
        cell["x"], cell["y"] = xs[col], ys[row]


def c_string(text):
    # Escaping '?' also prevents trigraphs in strict C99 compilers.
    return json.dumps(text).replace("?", r"\?")


def text_initializer(fields, root_id, indices, x, y, parent_width, parent_height):
    prefix, reference, suffix = fields.get("text", ("", None, ""))
    index = -1
    if reference is not None:
        token, path = reference
        if root_id and path.startswith(root_id + "."):
            path = path[len(root_id) + 1:]
        if path not in indices:
            raise token.error(f"unknown root integer property {reference[1]}")
        index = indices[path]
    anchors = []
    if "anchors.centerIn" in fields or "anchors.horizontalCenter" in fields:
        anchors.append("EQML_HCENTER")
    if "anchors.centerIn" in fields or "anchors.verticalCenter" in fields:
        anchors.append("EQML_VCENTER")
    return (f'    {{ .kind = EQML_TEXT, .data.text = {{\n'
            f'      .prefix = {c_string(prefix)}, .suffix = {c_string(suffix)}, .binding = {index},\n'
            f'      .x = {x}, .y = {y}, '
            f'.color = 0x{fields.get("color", 0):04x}, '
            f'.pixel_size = {fields.get("font.pixelSize", 14)}, .anchors = {" | ".join(anchors) or "0"},\n'
            f'      .parent_width = {parent_width}, .parent_height = {parent_height} }} }},')


def paint_items(root, indices):
    def visit(item, x, y, parent_width, parent_height):
        fields = item.fields
        x += fields.get("x", 0)
        y += fields.get("y", 0)
        if item.kind == "Text":
            yield text_initializer(fields, root.fields.get("id"), indices, x, y, parent_width, parent_height)
            return
        width, height = fields.get("width", 0), fields.get("height", 0)
        if item.kind == "Grid":
            if "anchors.centerIn" in fields or "anchors.horizontalCenter" in fields:
                x += (parent_width + 1) // 2 - (width + 1) // 2
            if "anchors.centerIn" in fields or "anchors.verticalCenter" in fields:
                y += (parent_height + 1) // 2 - (height + 1) // 2
        children = sorted(item.children, key=lambda child: child.fields.get("z", 0))
        # Qt stacks siblings as subtrees. Negative-z children precede their parent.
        split = next((i for i, child in enumerate(children) if child.fields.get("z", 0) >= 0), len(children))
        for child in children[:split]:
            yield from visit(child, x, y, width, height)
        # The renderer already clears the scene to the root color before painting.
        if item.kind == "Rectangle" and width and height and (item is not root or split):
            yield (f'    {{ .kind = EQML_RECT, .data.rect = {{ .x = {x}, .y = {y}, '
                   f'.width = {width}, .height = {height}, .color = 0x{fields.get("color", 0xffff):04x} }} }},')
        for child in children[split:]:
            yield from visit(child, x, y, width, height)

    return list(visit(root, 0, 0, 0, 0))


def compile_scene(source, name="app"):
    """Return a complete C99 translation unit; raise QmlError for unsupported input."""
    if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", name):
        raise QmlError("scene name must begin with a lowercase ASCII letter and contain only identifier characters")
    root, properties = Parser(source).scene()
    fields = root.fields
    items = paint_items(root, {key: i for i, key in enumerate(properties)})
    lines = ['/* Generated by EmberBSD qml-compile. */', '#include "eqml.h"',
             '#if EQML_SCENE_ABI != 2', '#error "Expected EmberBSD QML scene ABI 2"', '#endif', '']
    if properties:
        lines.append("static const struct eqml_int properties[] = {")
        lines.extend(f'    {{ .name = {c_string(key)}, .initial = {value} }},' for key, value in properties.items())
        lines.extend(["};", ""])
    if items:
        lines.append("static const struct eqml_item items[] = {")
        lines.extend(items)
        lines.extend(["};", ""])
    lines.extend([f"const struct eqml_scene eqml_{name} = {{",
                  f'    .width = {fields["width"]}, .height = {fields["height"]}, '
                  f'.background = 0x{fields.get("color", 0xffff):04x},',
                  f'    .items = {"items" if items else "NULL"}, .item_count = {len(items)},',
                  f'    .properties = {"properties" if properties else "NULL"}, .property_count = {len(properties)}',
                  "};", ""])
    return "\n".join(lines)
