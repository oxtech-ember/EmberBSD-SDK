# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compile the EmberBSD embedded QML profile to the eqml scene ABI."""
from dataclasses import dataclass
import json
import re

from emberbsd_sdk.qml_script import (Parser as ScriptParser, QmlScriptError,
                                     canonical_signal, compile_expression,
                                     compile_handler, emit_dispatch, signal_enum)


class QmlError(ValueError):
    pass


TOKEN = re.compile(
    r'(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"(?:[^"\\\n\r]|\\.)*"|\'(?:[^\'\\\n\r]|\\.)*\')'
    r'|(?P<int>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)'
    r'|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)|(?P<punct>.)'
)


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    column: int
    offset: int
    end: int

    def error(self, message):
        return QmlError(f"{self.line}:{self.column}: {message}")


@dataclass
class Item:
    kind: str
    fields: dict
    children: list
    handlers: dict
    properties: dict
    behaviors: dict


@dataclass(frozen=True)
class RawValue:
    source: str
    token: Token


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
            yield Token(value, line, column, pos, match.end())
        if "\n" in value:
            line += value.count("\n")
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)
        pos = match.end()
    yield Token("", line, column, len(source), len(source))


class Parser:
    def __init__(self, source):
        self.source = source
        self.stream = list(tokens(source))
        self.index = 0
        self.last = self.current
        self.ids = set()
        self.root_signals = set()
        self.static_bools = {}
        self.static_bool_uses = set()

    @property
    def current(self):
        return self.stream[self.index]

    def take(self, expected=None):
        token = self.current
        if not token.text or (expected is not None and token.text != expected):
            raise token.error(f"expected {expected or 'a value'}, got {token.text or 'end of input'}")
        self.last = token
        self.index += 1
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

    def raw_value(self):
        start = self.current
        if not start.text:
            raise start.error("expected a property value")
        depths = {"{": 0, "(": 0, "[": 0}
        pairs = {"}": "{", ")": "(", "]": "["}
        conditional_depth = 0
        end = start.end
        previous = start
        while self.current.text:
            token = self.current
            if not any(depths.values()):
                if token.text in (";", "}") or token.line != previous.line:
                    break
                if (self.index > 0 and
                    conditional_depth == 0 and
                    re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token.text) and
                    self.index + 1 < len(self.stream) and self.stream[self.index + 1].text == ":"):
                    break
            if token.text == "?":
                conditional_depth += 1
            elif token.text == ":" and conditional_depth:
                conditional_depth -= 1
            if token.text in depths:
                depths[token.text] += 1
            elif token.text in pairs:
                opener = pairs[token.text]
                if depths[opener] == 0:
                    break
                depths[opener] -= 1
            end = token.end
            previous = self.take()
        if end <= start.offset:
            raise start.error("expected a property value")
        return RawValue(self.source[start.offset:end].strip(), start)

    def grouped_fields(self, item_fields, prefix):
        self.take("{")
        while self.current.text != "}":
            start = self.current
            name = self.path()
            key = f"{prefix}.{name}"
            if key in item_fields:
                raise start.error(f"duplicate field {key}")
            self.take(":")
            if name in ("top", "left", "right", "bottom", "fill", "horizontalCenter",
                        "verticalCenter", "centerIn"):
                item_fields[key] = self.raw_value()
            elif name == "margins":
                item_fields[key] = self.raw_value()
            else:
                raise start.error(f"unsupported grouped property {key}")
            self.finish_field()
        self.take("}")

    def text_item(self):
        fields, handlers = {}, {}
        self.take("{")
        while self.current.text != "}":
            start = self.current
            key = self.path()
            if key in fields or key in handlers:
                raise start.error(f"duplicate Text field {key}")
            if key == "anchors" and self.current.text == "{":
                self.grouped_fields(fields, "anchors")
                self.finish_field()
                continue
            self.take(":")
            if key.startswith("on"):
                handlers[key] = self.raw_value()
            elif key == "text" and self.current.text == "qsTr":
                value = self.raw_value()
            elif key == "text":
                value = self.binding()
            elif key == "id":
                value = self.object_id()
            elif key in ("x", "y", "visible"):
                value = self.raw_value()
            elif key == "z":
                value = self.integer(-(2**31), 2**31 - 1)
            elif key == "color":
                value = self.color()
            elif key == "font.pixelSize":
                value = self.integer(7, 64)
            elif key in ANCHORS:
                value = self.raw_value()
            elif key.startswith("anchors."):
                fields[key] = self.raw_value()
                self.finish_field()
                continue
            else:
                raise start.error(f"unsupported Text field {key}")
            if not key.startswith("on"):
                fields[key] = value
            self.finish_field()
        end = self.take("}")
        check_anchors(fields, end)
        return Item("Text", fields, [], handlers, {}, {})

    def container(self, kind, depth, properties):
        if depth > 64:
            raise self.current.error("at most 64 nested containers are supported")
        self.take("{")
        fields, children, handlers, declared, behaviors = {}, [], {}, {}, {}
        while self.current.text != "}":
            start = self.name()
            key = start.text
            while self.current.text == ".":
                self.take(".")
                key += "." + self.name().text
            if key in ("Text", "Rectangle", "Item", "Grid", "Row", "MouseArea", "Image"):
                if kind == "Image":
                    raise start.error("Image children are unsupported")
                if kind in ("Grid", "Row") and key != "Rectangle":
                    raise start.error(f"{kind} places fixed-size Rectangle children")
                item = self.text_item() if key == "Text" else self.container(key, depth + 1, properties)
                if kind in ("Grid", "Row") and ("x" in item.fields or "y" in item.fields):
                    raise start.error(f"{kind} determines the x and y of its children")
                children.append(item)
                if self.current.text == ";":
                    self.take(";")
                continue
            if key == "signal":
                if depth:
                    raise start.error("application signals must be declared on the root")
                token = self.name()
                signal = token.text
                if (not re.fullmatch(r"[a-z][A-Za-z0-9_]*", signal) or
                    signal in RESERVED or signal in SIGNAL_KEYWORDS or signal in self.root_signals):
                    raise token.error(f"invalid or duplicate root signal {signal}")
                self.take("(")
                if self.current.text != ")":
                    raise self.current.error("root signals take no parameters")
                self.take(")")
                self.root_signals.add(signal)
            elif key == "property":
                type_token = self.name()
                token = self.name()
                property_name = token.text
                if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", property_name):
                    raise token.error(f"invalid property name {property_name}")
                if property_name == "visible":
                    raise token.error("visible is a built-in boolean property")
                if kind == "MouseArea" and type_token.text == "string":
                    if property_name in declared:
                        raise token.error(f"duplicate property {property_name}")
                    declared[property_name] = "string"
                    if self.current.text == ":":
                        self.take(":")
                        if self.current.text not in ("\n", "}", ";"):
                            raise self.current.error("initialized MouseArea string properties are unsupported")
                elif depth == 0 and type_token.text in ("int", "bool"):
                    if (property_name in RESERVED or property_name in properties or
                        property_name in self.static_bools):
                        raise token.error(f"invalid or duplicate integer property {property_name}")
                    self.take(":")
                    if type_token.text == "int":
                        properties[property_name] = self.integer(-(2**31), 2**31 - 1)
                    elif self.current.text in ("true", "false"):
                        self.static_bools[property_name] = int(self.take().text == "true")
                    else:
                        raise self.current.error("root boolean property requires a literal true or false")
                else:
                    raise start.error("only root integer/boolean and MouseArea string properties are supported")
            elif key == "anchors" and self.current.text == "{":
                self.grouped_fields(fields, "anchors")
                self.finish_field()
                continue
            elif key == "Behavior":
                self.take("on")
                axis = self.name().text
                if axis not in ("x", "y"):
                    raise start.error("Behavior supports only x or y")
                if axis in behaviors:
                    raise start.error(f"duplicate Behavior on {axis}")
                self.take("{")
                if self.name().text != "NumberAnimation":
                    raise self.last.error("Behavior requires NumberAnimation")
                self.take("{")
                duration = None
                while self.current.text != "}":
                    field = self.name()
                    if field.text != "duration" or duration is not None:
                        raise field.error("NumberAnimation supports only one duration")
                    self.take(":")
                    duration = self.integer(0, 2**31 - 1)
                    self.finish_field()
                self.take("}")
                self.finish_field()
                self.take("}")
                if duration is None:
                    raise start.error("NumberAnimation requires duration")
                behaviors[axis] = duration
                self.finish_field()
                continue
            else:
                if key in fields or key in handlers:
                    raise start.error(f"duplicate {kind} field {key}")
                if self.current.text == "{" and key.startswith("anchors"):
                    self.grouped_fields(fields, key)
                    self.finish_field()
                    continue
                if self.current.text == "{":
                    raise start.error(f"unsupported object or grouped property {key}")
                self.take(":")
                if key.startswith("on"):
                    handlers[key] = self.raw_value()
                elif key == "id":
                    fields[key] = self.object_id()
                elif kind == "Image" and key == "source":
                    literal = self.current
                    fields[key] = (literal, self.string())
                elif kind in ("Rectangle", "Item", "Image") and key in ("width", "height"):
                    fields[key] = self.integer(0 if depth else 1, 4096)
                elif kind == "Rectangle" and key == "color":
                    fields[key] = self.color()
                elif key == "objectName":
                    fields[key] = self.string()
                elif (kind == "Rectangle" and key == "opacity") or (
                    kind in ("Rectangle", "Item", "MouseArea", "Image") and key == "visible"):
                    fields[key] = self.raw_value()
                elif kind == "Grid" and key == "columns":
                    fields[key] = self.integer(1, 4096)
                elif kind == "Grid" and key == "spacing":
                    fields[key] = self.integer(0, 4096)
                elif kind == "Grid" and key in ANCHORS:
                    fields[key] = self.raw_value()
                elif kind == "Row" and key == "spacing":
                    fields[key] = self.integer(0, 4096)
                elif kind == "Row" and key == "layoutDirection":
                    fields[key] = self.raw_value()
                elif kind == "Row" and key in ANCHORS:
                    fields[key] = self.raw_value()
                elif (kind == "MouseArea" and key in (
                    "acceptedButtons", "hoverEnabled", "anchors.fill", "drag.target",
                    "drag.axis", "drag.minimumX", "drag.maximumX", "drag.minimumY",
                    "drag.maximumY")):
                    fields[key] = self.raw_value()
                elif key.startswith("anchors."):
                    fields[key] = self.raw_value()
                elif key in ("x", "y"):
                    fields[key] = self.raw_value()
                elif depth and key == "z" and kind in ("Rectangle", "Item", "MouseArea", "Image"):
                    fields[key] = self.integer(-(2**31), 2**31 - 1)
                else:
                    raise start.error(f"unsupported {kind} field {key}")
            self.finish_field()
        end = self.take("}")
        if depth == 0:
            for key in ("width", "height"):
                if key not in fields:
                    raise end.error(f"{kind} requires {key}")
            root_properties = properties.keys() | self.static_bools.keys()
            if self.root_signals:
                if "id" not in fields:
                    raise end.error("root signal declarations require a root id")
                members = ITEM_PROPERTIES | root_properties
                if kind == "Rectangle":
                    members |= RECTANGLE_PROPERTIES
                occupied = self.ids | members | ITEM_CALLS | {name + "Changed" for name in members}
                conflicts = self.root_signals & occupied
                if conflicts:
                    raise end.error(f"root signal name already in use: {min(conflicts)}")
            ambiguous = self.ids.intersection(root_properties)
            if ambiguous:
                raise end.error(f"object id also names a root property: {min(ambiguous)}")
        if kind in ("Grid", "Row"):
            check_anchors(fields, end)
            if kind == "Grid":
                place_grid(fields, children, end)
        return Item(kind, fields, children, handlers, declared, behaviors)

    def scene(self):
        self.take("import")
        self.take("QtQuick")
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", self.current.text):
            version = self.take()
            if version.text != "2.0":
                raise version.error("only QtQuick 2.0 or an unversioned QtQuick import is supported")
        self.finish_field()
        start = self.name()
        if start.text not in ("Rectangle", "Item"):
            raise start.error("scene root must be Rectangle or Item")
        properties = {}
        root = self.container(start.text, 0, properties)
        def place_rows(item):
            for child in item.children:
                place_rows(child)
            if item.kind == "Row":
                used = place_row(item.fields, item.children, self.last,
                                 self.static_bools)
                if used is not None:
                    self.static_bool_uses.add(used)
        place_rows(root)
        if self.current.text:
            raise self.current.error("unexpected content after the scene")
        return root, properties


ANCHORS = {"anchors.horizontalCenter": "parent.horizontalCenter",
           "anchors.verticalCenter": "parent.verticalCenter", "anchors.centerIn": "parent"}
RESERVED = {"id", "parent", "width", "height", "color", "anchors", "property", "import",
            "int", "x", "y", "z", "text", "font", "true", "false", "null", "undefined"}


# These QML / JavaScript tokens cannot declare a signal name.
SIGNAL_KEYWORDS = set("""
as break case catch class const continue debugger default delete do else enum export
extends finally for from function get if implements in instanceof interface let new of
on package private protected public readonly required return set signal static super
switch this throw try typeof var void while with
""".split())

# Public Item / Rectangle names already used by Qt. A new application signal
# must not capture a call or property notification belonging to these types.
ITEM_PROPERTIES = set("""
activeFocus activeFocusOnTab anchors antialiasing baseline baselineOffset bottom children
childrenRect clip containmentMask data enabled focus focusPolicy height horizontalCenter
implicitHeight implicitWidth layer left objectName opacity palette parent resources right
rotation scale smooth state states top transform transformOrigin transformOriginPoint
transitions verticalCenter visible visibleChildren width x y z
""".split())
RECTANGLE_PROPERTIES = set("""
border bottomLeftRadius bottomRightRadius color gradient radius topLeftRadius topRightRadius
""".split())
ITEM_CALLS = set("""
childAt contains destroy destroyed dumpItemTree ensurePolished forceActiveFocus grabToImage
mapFromGlobal mapFromItem mapToGlobal mapToItem nextItemInFocusChain paletteCreated
update windowChanged
""".split())


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


def place_row(fields, children, end, properties):
    cells = [child.fields for child in children
             if child.fields.get("width", 0) and child.fields.get("height", 0)]
    spacing = fields.get("spacing", 0)
    direction = fields.get("layoutDirection")
    reverse = False
    used = None
    if direction is not None:
        value = direction.source
        match = re.fullmatch(
            r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\?\s*Qt\.RightToLeft\s*:\s*Qt\.LeftToRight\s*",
            value)
        if match is not None and match.group(1) in properties:
            reverse = bool(properties[match.group(1)])
            used = match.group(1)
        elif value.strip() == "Qt.RightToLeft":
            reverse = True
        elif value.strip() != "Qt.LeftToRight":
            raise direction.token.error("Row layoutDirection requires a supported static Qt direction")
    if not cells:
        fields["width"], fields["height"] = 0, 0
        return used
    offsets = []
    extent = 0
    for index, cell in enumerate(cells):
        if index:
            extent += spacing
        offsets.append(extent)
        extent += cell["width"]
    height = max(cell["height"] for cell in cells)
    if max(extent, height) > 4096:
        raise end.error("implicit Row size exceeds 4096 pixels")
    fields["width"], fields["height"] = extent, height
    for cell, x in zip(cells, offsets):
        cell["x"] = extent - x - cell.get("width", 0) if reverse else x
        cell["y"] = 0
    return used


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
        if item.kind in ("Grid", "Row"):
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
        if item.kind == "Image":
            yield (f'    {{ .kind = EQML_IMAGE, .data.image = {{ .x = {x}, .y = {y}, '
                   f'.bitmap = &{fields["bitmap"]} }} }},')
        for child in children[split:]:
            yield from visit(child, x, y, width, height)

    return list(visit(root, 0, 0, 0, 0))


def _script_path(expression):
    parts = []
    while expression.op == "member":
        parts.append(expression.value)
        expression = expression.args[0]
    if expression.op != "name":
        return None
    return ".".join([expression.value, *reversed(parts)])


def _constant_expression(expression, geometry=None, locals_=None):
    geometry = geometry or {}
    locals_ = locals_ or {}
    if expression.op == "literal" and type(expression.value) in (int, float):
        return expression.value
    if expression.op == "name" and expression.value in locals_:
        value = locals_[expression.value]
        return int(value["code"]) if isinstance(value, dict) else int(value)
    if expression.op == "member":
        path = _script_path(expression)
        if path in geometry:
            value = geometry[path]
            if isinstance(value, (int, float)):
                return value
    if expression.op == "unary" and expression.value in ("+", "-"):
        value = _constant_expression(expression.args[0], geometry, locals_)
        return value if expression.value == "+" else -value
    if expression.op == "binary" and expression.value in ("+", "-"):
        left, right = (_constant_expression(arg, geometry, locals_) for arg in expression.args)
        return left + right if expression.value == "+" else left - right
    raise ValueError("expression is not a constant arithmetic value")


def _raw(value):
    return value.source if isinstance(value, RawValue) else str(value)


def _expression_constant(raw, *, nodes=None, geometry=None, locals_=None):
    try:
        tree = ScriptParser(_raw(raw)).expression_source()
        expression = compile_expression(_raw(raw), nodes=nodes, geometry=geometry,
                                       locals_=locals_)
        if expression.type != "int" or expression.prelude:
            raise ValueError("expected a static integer expression")
        value = _constant_expression(tree, geometry, locals_)
        if not isinstance(value, int):
            raise ValueError("expected an integer expression")
        return value
    except (QmlScriptError, SyntaxError, ValueError) as error:
        token = raw.token if isinstance(raw, RawValue) else None
        raise (token.error(str(error)) if token else QmlError(str(error))) from None


def _interactive_nodes(root):
    nodes = []
    parents = []

    def visit(item, parent):
        if item.kind in ("Grid", "Row"):
            raise QmlError(f"{item.kind} cannot be combined with interactive scene nodes")
        index = len(nodes)
        nodes.append(item)
        parents.append(parent)
        children = sorted(item.children, key=lambda child: child.fields.get("z", 0))
        for child in children:
            if child.fields.get("z", 0) < 0:
                raise QmlError("negative z is unsupported in parent-first interactive scene order")
            visit(child, index)

    visit(root, -1)
    return nodes, parents


def _check_layout_cycles(parents, dependencies):
    edges = [[] for _ in parents]
    for index, parent in enumerate(parents):
        if parent >= 0:
            edges[parent].append(index)
    for index, target in dependencies.items():
        edges[target].append(index)
    state = [0] * len(edges)

    def visit(index):
        if state[index] == 1:
            raise QmlError("interactive anchors form a layout cycle")
        if state[index] == 2:
            return
        state[index] = 1
        for dependent in edges[index]:
            visit(dependent)
        state[index] = 2

    for index in range(len(edges)):
        visit(index)


def _anchor_source(fields, name):
    value = fields.get("anchors." + name)
    return _raw(value) if value is not None else None


def _string_expression(value, nodes, slots):
    if value is None:
        return None
    if isinstance(value, tuple):
        prefix, reference, suffix = value
        if reference is not None:
            raise QmlError("integer model bindings are not supported in interactive text")
        literal = prefix + suffix
        if len(literal) > 255:
            raise QmlError("interactive Text initial text is limited to 255 printable bytes")
        return c_string(literal)
    try:
        tree = ScriptParser(_raw(value)).expression_source()
        expression = compile_expression(_raw(value), nodes=nodes, slots=slots)
    except QmlScriptError as error:
        token = value.token if isinstance(value, RawValue) else None
        raise (token.error(str(error)) if token else QmlError(str(error))) from None
    literal = _constant_string(tree)
    if expression.type != "string" or expression.prelude or literal is None:
        token = value.token if isinstance(value, RawValue) else None
        raise (token.error("interactive labels require static printable text") if token
               else QmlError("interactive labels require static printable text"))
    if len(literal) > 255:
        token = value.token if isinstance(value, RawValue) else None
        raise (token.error("interactive Text initial text is limited to 255 printable bytes") if token
               else QmlError("interactive Text initial text is limited to 255 printable bytes"))
    return c_string(literal)


def _constant_string(expression):
    if expression.op == "literal" and isinstance(expression.value, str):
        return expression.value
    if expression.op == "call" and expression.args[0].op == "name" and \
        expression.args[0].value == "qsTr" and len(expression.args) == 2:
        return _constant_string(expression.args[1])
    if expression.op == "binary" and expression.value == "+":
        left = _constant_string(expression.args[0])
        right = _constant_string(expression.args[1])
        if left is not None and right is not None:
            return left + right
    return None


def _referenced_text_targets(source):
    tree, _ = ScriptParser(source).handler()
    targets = set()

    def visit(node):
        if not hasattr(node, "op"):
            return
        if node.op == "member":
            path = _script_path(node)
            if path and path.endswith(".text"):
                targets.add(path.rsplit(".", 1)[0])
        if hasattr(node, "value"):
            visit(node.value)
        for child in node.args:
            visit(child)

    visit(tree)
    return targets


def _interactive_opacity(raw, nodes, ids):
    try:
        tree = ScriptParser(raw.source).expression_source()
        binding = compile_expression(raw.source, nodes=ids)
    except QmlScriptError as error:
        raise raw.token.error(str(error)) from None
    if tree.op == "literal" and type(tree.value) in (int, float):
        if not 0 <= tree.value <= 1:
            raise raw.token.error("opacity must be between zero and one")
        return -1, 0, int((1 - tree.value) * 255 + 0.5)
    condition, pressed, unpressed = tree.args if tree.op == "conditional" else (None, None, None)
    area_name = None
    if (condition is not None and condition.op == "member" and
        condition.value == "containsPress" and condition.args[0].op == "name"):
        area_name = condition.args[0].value
    if (binding.type != "float" or binding.prelude or area_name not in ids or
        (area_name in ids and nodes[ids[area_name]].kind != "MouseArea") or
        pressed.op != "literal" or type(pressed.value) not in (int, float) or
        unpressed.op != "literal" or type(unpressed.value) not in (int, float) or
        unpressed.value != 1.0):
        raise raw.token.error("opacity supports a containsPress ternary with opaque default")
    opacity = float(pressed.value)
    if not 0 <= opacity <= 1:
        raise raw.token.error("opacity must be between zero and one")
    return ids[area_name], int((1.0 - opacity) * 255 + 0.5), 0


def interactive_scene(root, properties, name, bitmap_lines, bindings, root_signals):
    signal_paths = {f'{root.fields["id"]}.{signal}': signal for signal in root_signals}
    if properties:
        raise QmlError("integer model properties cannot be combined with an interactive scene")
    nodes, parents = _interactive_nodes(root)
    if len(nodes) > 256:
        raise QmlError("interactive scenes are limited to 256 nodes")
    ids = {}
    for index, item in enumerate(nodes):
        ident = item.fields.get("id")
        if ident:
            ids[ident] = index
    geometry = {}
    for item in nodes:
        ident = item.fields.get("id")
        if ident:
            for dimension in ("width", "height", "x", "y"):
                value = item.fields.get(dimension)
                if isinstance(value, int):
                    geometry[f"{ident}.{dimension}"] = value

    coordinates = {}
    for index, item in enumerate(nodes):
        ident = item.fields.get("id")
        if not ident:
            if item.behaviors:
                raise QmlError("Behavior requires an identified Rectangle")
            continue
        if item.behaviors and item.kind != "Rectangle":
            raise QmlError("Behavior is supported only on Rectangle coordinates")
        if item.kind != "Rectangle":
            continue
        fields = item.fields
        anchored = {
            "x": any(key in fields for key in ("anchors.fill", "anchors.left", "anchors.right",
                                                 "anchors.horizontalCenter", "anchors.centerIn")),
            "y": any(key in fields for key in ("anchors.fill", "anchors.top", "anchors.bottom",
                                                 "anchors.verticalCenter", "anchors.centerIn")),
        }
        for axis in ("x", "y"):
            if axis in item.behaviors and anchored[axis]:
                raise QmlError(f"Behavior on {axis} conflicts with anchors")
            coordinates[f"{ident}.{axis}"] = {
                "node": index,
                "axis": "EQML_AXIS_X" if axis == "x" else "EQML_AXIS_Y",
                "duration_ms": item.behaviors.get(axis, 0),
                "writable": not anchored[axis],
                "anchor_error": f"{axis} is controlled by anchors",
            }

    # Find Texts whose values can be changed by handlers before assigning the
    # stable string-slot indices used by both the node and generated callbacks.
    dynamic_texts = set()
    for item in nodes:
        if item.kind == "Text" and ("text" not in item.fields or item.handlers):
            dynamic_texts.add(item.fields.get("id"))
        for raw in item.handlers.values():
            try:
                dynamic_texts.update(_referenced_text_targets(raw.source))
            except QmlScriptError as error:
                raise raw.token.error(str(error)) from None
    slots = {f"{ident}.visible": {"type": "bool", "visible_node": index}
             for ident, index in ids.items()}
    text_slot = {}
    area_slot = {}
    slot_count = 0
    for index, item in enumerate(nodes):
        ident = item.fields.get("id")
        if item.kind == "Text" and ident in dynamic_texts:
            text_slot[index] = slot_count
            if ident:
                slots[f"{ident}.text"] = {"index": slot_count, "type": "string"}
            slot_count += 1
        if item.kind == "MouseArea":
            for property_name, property_type in item.properties.items():
                if property_type != "string":
                    raise QmlError(f"unsupported MouseArea property type {property_type}")
                area_slot[(index, property_name)] = slot_count
                if ident:
                    slots[f"{ident}.{property_name}"] = {"index": slot_count, "type": "string"}
                slot_count += 1
    if slot_count > 256:
        raise QmlError("interactive scenes are limited to 256 string slots")

    callbacks = []
    dispatch = {}
    signal_masks = [set() for _ in nodes]
    for index, item in enumerate(nodes):
        for key, handler in item.handlers.items():
            try:
                canonical = canonical_signal(key)
                signal = signal_enum(canonical)
                if item.kind == "MouseArea" and canonical == "onTextChanged":
                    raise QmlScriptError("MouseArea has no textChanged signal")
                if item.kind == "Text" and canonical != "onTextChanged":
                    raise QmlScriptError("Text supports only onTextChanged")
                if item.kind not in ("Text", "MouseArea"):
                    raise QmlScriptError("this item kind does not support handlers")
                local_slots = dict(slots)
                if item.kind == "Text" and index in text_slot:
                    local_slots["text"] = {"index": text_slot[index], "type": "string"}
                for property_name in item.properties:
                    local_slots[property_name] = {
                        "index": area_slot[(index, property_name)], "type": item.properties[property_name]}
                function = f"_eqml_handler_{index}_{len(callbacks)}"
                allow_mouse = canonical not in ("onEntered", "onExited", "onTextChanged")
                callbacks.append(compile_handler(handler.source, function,
                    current_node=index, nodes=ids, slots=local_slots,
                    allow_mouse=allow_mouse, coordinates=coordinates, signals=signal_paths))
                dispatch[(index, signal)] = function
                signal_masks[index].add(signal)
            except QmlScriptError as error:
                raise handler.token.error(str(error)) from None

    # Fill node records after all ids and text slots are known. Geometry used
    # in arithmetic bindings is static for this profile except drag motion.
    records = []
    bottom_dependencies = {}
    root_fields = root.fields
    for index, item in enumerate(nodes):
        fields = item.fields
        parent_index = parents[index]
        parent = nodes[parent_index] if parent_index >= 0 else None
        item_geometry = dict(geometry)
        item_geometry["parent.width"] = parent.fields.get("width", 0) if parent else 0
        item_geometry["parent.height"] = parent.fields.get("height", 0) if parent else 0
        locals_ = {"width": {"type": "int", "code": str(fields.get("width", 0))},
                   "height": {"type": "int", "code": str(fields.get("height", 0))}}

        width = fields.get("width", 0)
        height = fields.get("height", 0)
        x = fields.get("x", 0)
        y = fields.get("y", 0)
        if isinstance(x, RawValue):
            x = _expression_constant(x, nodes=ids, geometry=item_geometry, locals_=locals_)
        if isinstance(y, RawValue):
            y = _expression_constant(y, nodes=ids, geometry=item_geometry, locals_=locals_)
        if not isinstance(x, int) or not isinstance(y, int):
            raise QmlError("node x and y must resolve to integer coordinates")
        if not -(2**31) <= x < 2**31 or not -(2**31) <= y < 2**31:
            raise QmlError("node coordinates must fit signed 32-bit integers")

        anchors = 0
        bottom_edge = "EQML_EDGE_NONE"
        bottom_target = -1
        bottom_margin = 0
        margin = fields.get("anchors.margins", 0)
        if isinstance(margin, RawValue):
            margin = _expression_constant(margin, nodes=ids, geometry=item_geometry)
        if not isinstance(margin, int) or not 0 <= margin <= 32767:
            raise QmlError("anchor margins must be an integer in 0..32767")
        fill = _anchor_source(fields, "fill")
        top = _anchor_source(fields, "top")
        left = _anchor_source(fields, "left")
        center = _anchor_source(fields, "centerIn")
        hcenter = _anchor_source(fields, "horizontalCenter")
        vcenter = _anchor_source(fields, "verticalCenter")
        bottom = _anchor_source(fields, "bottom")
        right = _anchor_source(fields, "right")
        if right is not None:
            raise QmlError("anchors.right is unsupported in the interactive profile")
        if parent is None and any(value is not None for value in
                                  (fill, top, left, center, hcenter, vcenter, bottom)):
            raise QmlError("interactive anchors require a parent item")
        if fill is not None:
            if fill != "parent" or item.kind != "MouseArea" or any(
                value is not None for value in (top, left, center, hcenter, vcenter, bottom)):
                raise QmlError("anchors.fill supports only MouseArea anchors.fill: parent")
            if parent.kind not in ("Rectangle", "Item"):
                raise QmlError("anchors.fill requires a fixed-size Rectangle or Item parent")
            x = y = 0
            width, height = parent.fields.get("width", 0), parent.fields.get("height", 0)
        if center is not None:
            if center != "parent" or any(value is not None for value in (hcenter, vcenter, top, left, bottom)):
                raise QmlError("anchors.centerIn supports only the immediate parent")
            anchors = 3
            x = y = 0
        if hcenter is not None:
            if hcenter != "parent.horizontalCenter" or left is not None:
                raise QmlError("anchors.horizontalCenter supports only the immediate parent")
            anchors |= 1
            x = 0
        if vcenter is not None:
            if vcenter != "parent.verticalCenter" or top is not None:
                raise QmlError("anchors.verticalCenter supports only the immediate parent")
            anchors |= 2
            y = 0
        if top is not None:
            if top != "parent.top" or anchors & 2:
                raise QmlError("anchors.top supports only an uncentered immediate parent")
            y = margin
        if left is not None:
            if left != "parent.left" or anchors & 1:
                raise QmlError("anchors.left supports only an uncentered immediate parent")
            x = margin
        if bottom is not None:
            if anchors & 2 or top is not None:
                raise QmlError("bottom anchors cannot be combined with vertical centering or top anchors")
            target, dot, edge = bottom.rpartition(".")
            if dot != "." or edge not in ("top", "bottom"):
                raise QmlError("bottom anchor target must end in .top or .bottom")
            if target == "parent":
                bottom_target = parent_index
            elif target in ids:
                bottom_target = ids[target]
            else:
                raise QmlError(f"unknown bottom anchor target {target}")
            if bottom_target < 0 or nodes[bottom_target].kind == "MouseArea":
                raise QmlError("bottom anchors must target a visual node")
            if bottom_target == index:
                raise QmlError("bottom anchor cannot target itself")
            bottom_dependencies[index] = bottom_target
            bottom_edge = "EQML_EDGE_TOP" if edge == "top" else "EQML_EDGE_BOTTOM"
            bottom_margin = margin
            y = 0
        if margin and top is None and left is None and bottom is None:
            raise QmlError("anchors.margins needs a positional anchor")

        color = fields.get("color", 0xffff if item.kind == "Rectangle" else 0)
        pixel_size = fields.get("font.pixelSize", 14)
        text = None
        if item.kind == "Text" and "text" in fields:
            text = _string_expression(fields["text"], ids, slots)

        opacity_area, pressed_transparency, transparency = -1, 0, 0
        if "opacity" in fields:
            opacity_area, pressed_transparency, transparency = _interactive_opacity(
                fields["opacity"], nodes, ids)
        hidden = 0
        if "visible" in fields:
            value = _raw(fields["visible"])
            if value not in ("true", "false"):
                raise fields["visible"].token.error("visible requires a boolean literal")
            hidden = int(value == "false")

        buttons = "EQML_BUTTON_LEFT"
        hover = 0
        drag_axes, drag_target = 0, -1
        min_x = max_x = min_y = max_y = 0
        if item.kind == "MouseArea":
            accepted = fields.get("acceptedButtons")
            if accepted is not None:
                accepted_value = _raw(accepted)
                if accepted_value == "Qt.AllButtons":
                    buttons = "EQML_BUTTON_ALL"
                else:
                    raise accepted.token.error("acceptedButtons supports Qt.AllButtons in this profile")
            if "hoverEnabled" in fields:
                value = _raw(fields["hoverEnabled"])
                if value not in ("true", "false"):
                    raise fields["hoverEnabled"].token.error("hoverEnabled must be a boolean literal")
                hover = int(value == "true")
            target = fields.get("drag.target")
            axis = fields.get("drag.axis")
            if target is not None or axis is not None:
                target_name = _raw(target) if target is not None else ""
                if target_name not in ids:
                    raise (target.token.error("drag.target must name an object id") if target else QmlError("drag.axis requires drag.target"))
                drag_target = ids[target_name]
                if nodes[drag_target].kind != "Rectangle":
                    raise (target.token.error("drag.target must name a Rectangle") if target
                           else QmlError("drag.target must name a Rectangle"))
                axis_name = _raw(axis) if axis is not None else "Drag.XAndYAxis"
                axes = {"Drag.XAxis": 1, "Drag.YAxis": 2, "Drag.XAndYAxis": 3}
                if axis_name not in axes:
                    raise (axis.token.error("unsupported drag.axis") if axis else QmlError("drag.target requires drag.axis"))
                drag_axes = axes[axis_name]
                for key, dest in (("drag.minimumX", "min_x"), ("drag.maximumX", "max_x"),
                                  ("drag.minimumY", "min_y"), ("drag.maximumY", "max_y")):
                    raw = fields.get(key)
                    if raw is not None:
                        value = _expression_constant(raw, nodes=ids, geometry=item_geometry,
                                                     locals_=locals_)
                        if dest == "min_x": min_x = value
                        elif dest == "max_x": max_x = value
                        elif dest == "min_y": min_y = value
                        else: max_y = value
                if drag_axes & 1:
                    if "drag.minimumX" not in fields or "drag.maximumX" not in fields:
                        raise QmlError("horizontal drag requires minimumX and maximumX")
                    if min_x > max_x:
                        raise QmlError("drag.minimumX must not exceed drag.maximumX")
                elif any(key in fields for key in ("drag.minimumX", "drag.maximumX")):
                    raise QmlError("horizontal drag limits require an X drag axis")
                if drag_axes & 2:
                    if "drag.minimumY" not in fields or "drag.maximumY" not in fields:
                        raise QmlError("vertical drag requires minimumY and maximumY")
                    if min_y > max_y:
                        raise QmlError("drag.minimumY must not exceed drag.maximumY")
                elif any(key in fields for key in ("drag.minimumY", "drag.maximumY")):
                    raise QmlError("vertical drag limits require a Y drag axis")
            elif any(key in fields for key in ("drag.minimumX", "drag.maximumX", "drag.minimumY", "drag.maximumY")):
                raise QmlError("drag limits require drag.target")
            if any(not -(2**31) <= value < 2**31 for value in (min_x, max_x, min_y, max_y)):
                raise QmlError("drag limits must fit signed 32-bit integers")

        signal_bits = " | ".join(f"(1U << {signal})" for signal in sorted(
            signal_masks[index], key=lambda value: list(("EQML_ENTERED", "EQML_EXITED", "EQML_PRESSED",
            "EQML_RELEASED", "EQML_CLICKED", "EQML_DOUBLE_CLICKED", "EQML_PRESS_AND_HOLD",
            "EQML_TEXT_CHANGED")).index(value))) or "0"
        text_slot_index = text_slot.get(index, -1)
        node_kind = {"Rectangle": "RECT", "Item": "ITEM", "Text": "TEXT", "MouseArea": "AREA",
                     "Image": "IMAGE"}[item.kind]
        records.append(
            "    { .kind = EQML_NODE_%s, .parent = %d, .x = %d, .y = %d, "
            ".width = %d, .height = %d, .color = 0x%04x, .pixel_size = %d, .anchors = %d, "
            ".bottom_edge = %s, .bottom_target = %d, .bottom_margin = %d, .text = %s, "
            ".text_slot = %d, .opacity_area = %d, .pressed_transparency = %d, "
            ".transparency = %d, .hidden = %d, .buttons = %s, .signals = %s, .hover = %d, .drag_axes = %d, .drag_target = %d, "
            ".min_x = %d, .max_x = %d, .min_y = %d, .max_y = %d%s }," % (
                node_kind, parent_index, x, y, width, height, color, pixel_size, anchors,
                bottom_edge, bottom_target, bottom_margin, text or "NULL", text_slot_index,
                opacity_area, pressed_transparency, transparency, hidden, buttons, signal_bits, hover, drag_axes, drag_target,
                min_x, max_x, min_y, max_y,
                ", .bitmap = &" + fields["bitmap"] if item.kind == "Image" else ""))

    if bindings is not None:
        for ident, index in ids.items():
            bindings.append((f"node_{ident}", index))
            if index in text_slot:
                bindings.append((f"text_{ident}", text_slot[index]))
            for axis, duration in nodes[index].behaviors.items():
                bindings.append((f"duration_{axis}_{ident}", duration))

    _check_layout_cycles(parents, bottom_dependencies)
    lines = ['/* Generated by EmberBSD qml-compile. */', '#include "eqml.h"',
             '#include "eqml_input.h"', '#include <errno.h>', '#include <stdio.h>',
             '#include <stdint.h>', '#include <string.h>',
             '#if EQML_SCENE_ABI != 5', '#error "Expected EmberBSD QML scene ABI 5"', '#endif', '',
             *bitmap_lines, "static const struct eqml_node nodes[] = {", *records, "};", ""]
    lines.extend(callbacks)
    if callbacks:
        lines.append("")
    if callbacks:
        try:
            lines.append(emit_dispatch("_eqml_dispatch", dispatch))
        except QmlScriptError as error:
            raise QmlError(str(error)) from None
        handler = "_eqml_dispatch"
        lines.append("")
    else:
        handler = "NULL"
    lines.extend(["static const struct eqml_program program = {",
                  "    .nodes = nodes, .node_count = sizeof(nodes) / sizeof(nodes[0]),",
                  f"    .string_count = {slot_count}, .handler = {handler}", "};", "",
                  f"const struct eqml_scene eqml_{name} = {{",
                  f"    .width = {root.fields['width']}, .height = {root.fields['height']}, "
                  ".background = 0xffff,",
                  "    .items = NULL, .item_count = 0, .properties = NULL, .property_count = 0,",
                  "    .program = &program", "};", ""])
    return "\n".join(lines)


def compile_scene(source, name="app", *, assets=None):
    """Return a complete C99 translation unit; raise QmlError for unsupported input."""
    return _compile_scene(source, name, assets, None)


def compile_scene_with_header(source, name="app", *, assets=None):
    """Return the C translation unit and its C application bindings header."""
    bindings = []
    result = _compile_scene(source, name, assets, bindings)
    guard = f"EMBERBSD_QML_SCENE_{name}_H"
    header = ["/* Generated by EmberBSD qml-compile. */", f"#ifndef {guard}", f"#define {guard}",
              "", '#include "eqml.h"', "", "#ifdef __cplusplus", 'extern "C" {', "#endif",
              f"extern const struct eqml_scene eqml_{name};", "#ifdef __cplusplus", "}", "#endif", ""]
    if bindings:
        header.append("enum {")
        header.extend(f"    eqml_{name}_{symbol} = {value}," for symbol, value in bindings)
        header.extend(["};", ""])
    header.extend([f"#endif /* {guard} */", ""])
    return result, "\n".join(header)


def _compile_scene(source, name, assets, bindings):
    if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", name):
        raise QmlError("scene name must begin with a lowercase ASCII letter and contain only identifier characters")
    parser = Parser(source)
    root, properties = parser.scene()
    for item in _walk(root):
        if item.kind != "Image":
            continue
        fields = item.fields
        if assets is None or "source" not in fields:
            raise QmlError("Image requires a local PNG source and an explicit asset loader")
        token, source_name = fields["source"]
        if any(key.startswith("anchors.") for key in fields):
            raise token.error("Image supports explicit x/y coordinates only")
        try:
            symbol, width, height = assets.load(source_name)
        except (OSError, ValueError) as error:
            raise token.error(str(error)) from None
        if fields.get("width", width) != width or fields.get("height", height) != height:
            raise token.error("Image width and height must match the PNG dimensions")
        fields.update(bitmap=symbol, width=width, height=height)
    bitmap_lines = assets.lines if assets else []
    interactive = any(item.kind == "MouseArea" for item in _walk(root))
    if interactive:
        if parser.static_bools:
            raise QmlError("root boolean layout properties are static-only and cannot be used in interactive scenes")
        return interactive_scene(root, properties, name, bitmap_lines, bindings, parser.root_signals)
    unused_bools = parser.static_bools.keys() - parser.static_bool_uses
    if unused_bools:
        raise QmlError("root boolean properties must select a supported static Row layout direction")
    for item in _walk(root):
        if item is not root and item.kind == "Item" and any(
                key.startswith("anchors.") for key in item.fields):
            raise QmlError("static nested Item supports only explicit geometry")
        if item.behaviors:
            raise QmlError("Behavior requires an interactive scene")
        if item.handlers:
            key, handler = next(iter(item.handlers.items()))
            raise handler.token.error(f"unsupported {item.kind} field {key}")
        if "visible" in item.fields:
            raise item.fields["visible"].token.error("visible requires an interactive scene")
        if "opacity" in item.fields:
            raise item.fields["opacity"].token.error("unsupported Rectangle field opacity")
    _resolve_static_items(root)
    fields = root.fields
    items = paint_items(root, {key: i for i, key in enumerate(properties)})
    lines = ['/* Generated by EmberBSD qml-compile. */', '#include "eqml.h"',
             '#if EQML_SCENE_ABI != 5', '#error "Expected EmberBSD QML scene ABI 5"', '#endif', '']
    lines.extend(bitmap_lines)
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
                  f'    .properties = {"properties" if properties else "NULL"}, .property_count = {len(properties)},',
                  '    .program = NULL',
                  "};", ""])
    return "\n".join(lines)


def _walk(root):
    yield root
    for child in root.children:
        yield from _walk(child)


def _resolve_static_items(root):
    for item in _walk(root):
        for key in ("x", "y"):
            value = item.fields.get(key)
            if isinstance(value, RawValue):
                try:
                    tree = ScriptParser(value.source).expression_source()
                    expression = compile_expression(value.source)
                    if expression.type != "int" or expression.prelude:
                        raise ValueError("expected a constant integer")
                    coordinate = _constant_expression(tree)
                    if not -32768 <= coordinate <= 32767:
                        raise ValueError("coordinate is outside -32768..32767")
                    item.fields[key] = coordinate
                except (QmlScriptError, SyntaxError, ValueError) as error:
                    raise value.token.error(str(error)) from None
        text = item.fields.get("text")
        if isinstance(text, RawValue):
            try:
                tree = ScriptParser(text.source).expression_source()
                expression = compile_expression(text.source)
                if expression.type != "string" or expression.prelude:
                    raise ValueError("expected constant text")
                literal = _constant_string(tree)
                if literal is None:
                    raise ValueError("expected constant text")
                item.fields["text"] = (literal, None, "")
            except (QmlScriptError, SyntaxError, ValueError) as error:
                raise text.token.error(str(error)) from None
