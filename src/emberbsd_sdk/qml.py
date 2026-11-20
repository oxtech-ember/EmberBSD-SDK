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
        if not re.fullmatch(r"#[a-fA-F0-9]{6}", rgb):
            raise token.error("expected an opaque #RRGGBB color")
        r, g, b = bytes.fromhex(rgb[1:])
        return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)

    def finish_field(self):
        if self.current.text == ";":
            self.take(";")
        elif self.current.text not in ("}", "") and self.current.line == self.last.line:
            raise self.current.error("separate fields with a newline or semicolon")

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
            elif key in ("x", "y"):
                value = self.integer(-32768, 32767)
            elif key == "color":
                value = self.color()
            elif key == "font.pixelSize":
                value = self.integer(7, 64)
            elif key in ANCHORS:
                value = self.path()
                if value != ANCHORS[key]:
                    raise start.error(f"{key} requires {ANCHORS[key]}")
            else:
                raise start.error(f"unsupported Text field {key}")
            fields[key] = value
            self.finish_field()
        end = self.take("}")
        hc = "anchors.horizontalCenter" in fields
        vc = "anchors.verticalCenter" in fields
        both = "anchors.centerIn" in fields
        if (both and (hc or vc)) or ((both or hc) and "x" in fields) or ((both or vc) and "y" in fields):
            raise end.error("overlapping anchor and position constraints")
        return fields

    def scene(self):
        self.take("import")
        self.take("QtQuick")
        self.finish_field()
        self.take("Rectangle")
        self.take("{")
        fields, properties, items = {}, {}, []
        while self.current.text != "}":
            start = self.name()
            key = start.text
            if key == "Text":
                items.append(self.text_item())
                if self.current.text == ";":
                    self.take(";")
                continue
            if key == "property":
                self.take("int")
                token = self.name()
                key = token.text
                if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", key) or key in RESERVED or key in properties:
                    raise token.error(f"invalid or duplicate integer property {key}")
                self.take(":")
                properties[key] = self.integer(-(2**31), 2**31 - 1)
            else:
                if key in fields:
                    raise start.error(f"duplicate Rectangle field {key}")
                self.take(":")
                if key == "id":
                    token = self.name()
                    if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", token.text) or token.text in RESERVED:
                        raise token.error("invalid root id")
                    fields[key] = token.text
                elif key in ("width", "height"):
                    fields[key] = self.integer(1, 4096)
                elif key == "color":
                    fields[key] = self.color()
                else:
                    raise start.error(f"unsupported Rectangle field {key}")
            self.finish_field()
        end = self.take("}")
        if self.current.text:
            raise self.current.error("unexpected content after the scene")
        for key in ("width", "height"):
            if key not in fields:
                raise end.error(f"Rectangle requires {key}")
        if fields.get("id") in properties:
            raise end.error("root id also names an integer property")
        return fields, properties, items


ANCHORS = {"anchors.horizontalCenter": "parent.horizontalCenter",
           "anchors.verticalCenter": "parent.verticalCenter", "anchors.centerIn": "parent"}
RESERVED = {"id", "parent", "width", "height", "color", "anchors", "property", "import",
            "int", "x", "y", "text", "font", "true", "false", "null", "undefined"}


def c_string(text):
    # Escaping '?' also prevents trigraphs in strict C99 compilers.
    return json.dumps(text).replace("?", r"\?")


def text_initializer(fields, root_id, indices):
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
    return (f'    {{ .prefix = {c_string(prefix)}, .suffix = {c_string(suffix)}, .binding = {index},\n'
            f'      .x = {fields.get("x", 0)}, .y = {fields.get("y", 0)}, '
            f'.color = 0x{fields.get("color", 0):04x}, '
            f'.pixel_size = {fields.get("font.pixelSize", 14)}, .anchors = {" | ".join(anchors) or "0"} }},')


def compile_scene(source, name="app"):
    """Return a complete C99 translation unit; raise QmlError for unsupported input."""
    if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", name):
        raise QmlError("scene name must begin with a lowercase ASCII letter and contain only identifier characters")
    fields, properties, items = Parser(source).scene()
    lines = ['/* Generated by EmberBSD qml-compile. */', '#include "eqml.h"',
             '#if EQML_SCENE_ABI != 1', '#error "Expected EmberBSD QML scene ABI 1"', '#endif', '']
    if properties:
        lines.append("static const struct eqml_int properties[] = {")
        lines.extend(f'    {{ .name = {c_string(key)}, .initial = {value} }},' for key, value in properties.items())
        lines.extend(["};", ""])
    if items:
        indices = {key: i for i, key in enumerate(properties)}
        lines.append("static const struct eqml_text texts[] = {")
        lines.extend(text_initializer(item, fields.get("id"), indices) for item in items)
        lines.extend(["};", ""])
    lines.extend([f"const struct eqml_scene eqml_{name} = {{",
                  f'    .width = {fields["width"]}, .height = {fields["height"]}, '
                  f'.background = 0x{fields.get("color", 0xffff):04x},',
                  f'    .texts = {"texts" if items else "NULL"}, .text_count = {len(items)},',
                  f'    .properties = {"properties" if properties else "NULL"}, .property_count = {len(properties)}',
                  "};", ""])
    return "\n".join(lines)
