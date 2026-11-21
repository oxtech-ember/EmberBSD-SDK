# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Parse and lower the bounded JavaScript subset used by embedded QML handlers.

This is a host compiler. No source text or interpreter is shipped to the target.
"""
from dataclasses import dataclass
import ast
import json
import re


class QmlScriptError(ValueError):
    pass


SIGNALS = {
    "onEntered": "EQML_ENTERED", "onExited": "EQML_EXITED",
    "onPressed": "EQML_PRESSED", "onReleased": "EQML_RELEASED",
    "onClicked": "EQML_CLICKED", "onDoubleClicked": "EQML_DOUBLE_CLICKED",
    "onPressAndHold": "EQML_PRESS_AND_HOLD", "onTextChanged": "EQML_TEXT_CHANGED",
}


def canonical_signal(name):
    if name not in SIGNALS:
        raise QmlScriptError(f"unsupported signal {name}")
    return name


def signal_enum(name):
    return SIGNALS[canonical_signal(name)]


TOKEN = re.compile(r"\s+|//[^\n]*|/\*[\s\S]*?\*/|(?:'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")|"
                   r"(?:\+=|-=|===|!==|==|!=|<=|>=|&&|\|\||=>)|(?:0[xX][0-9a-fA-F]+|(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+))|"
                   r"[A-Za-z_$][A-Za-z0-9_$]*|[{}().,:;?+&|=!<>-]")


def lex(source):
    result = []
    pos = 0
    while pos < len(source):
        match = TOKEN.match(source, pos)
        if match is None:
            raise QmlScriptError(f"unsupported expression near {source[pos:pos + 24]!r}")
        token = match.group()
        if not token.isspace() and not token.startswith("//") and not token.startswith("/*"):
            result.append(token)
        pos = match.end()
    result.append("<eof>")
    return result


@dataclass
class Expr:
    op: str
    value: object = None
    args: tuple = ()


@dataclass
class Stmt:
    op: str
    value: object = None
    args: tuple = ()


class Parser:
    def __init__(self, source):
        self.tokens = lex(source)
        self.i = 0

    @property
    def token(self):
        return self.tokens[self.i]

    def take(self, value=None):
        token = self.token
        if value is not None and token != value:
            raise QmlScriptError(f"expected {value}, got {token}")
        if token == "<eof>" and value is None:
            raise QmlScriptError("unexpected end of handler")
        self.i += 1
        return token

    def maybe(self, value):
        if self.token == value:
            self.i += 1
            return True
        return False

    def handler(self):
        # QML arrow handlers bind their single event argument to the canonical
        # local name `mouse`; a plain expression handler has no event argument.
        if self.token == "(":
            save = self.i
            self.take("(")
            arg = self.take()
            if self.maybe(")") and self.maybe("=>"):
                if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", arg):
                    raise QmlScriptError("handler argument must be an identifier")
                self.mouse_name = arg
            else:
                self.i = save
                self.mouse_name = None
        else:
            self.mouse_name = None
        if self.token == "{":
            result = self.block()
        else:
            result = self.statement()
            self.maybe(";")
        if self.token != "<eof>":
            raise QmlScriptError(f"unexpected token {self.token}")
        return result, self.mouse_name

    def block(self):
        self.take("{")
        statements = []
        while self.token != "}":
            if self.token == "<eof>":
                raise QmlScriptError("unterminated handler block")
            statements.append(self.statement())
            self.maybe(";")
        self.take("}")
        return Stmt("block", args=tuple(statements))

    def statement(self):
        if self.token == "{":
            return self.block()
        if self.token == "var":
            raise QmlScriptError("function-scoped var is outside the embedded profile")
        if self.token == "if":
            self.take("if")
            self.take("(")
            condition = self.expression()
            self.take(")")
            yes = self.statement()
            self.maybe(";")
            no = None
            if self.maybe("else"):
                no = self.statement()
            return Stmt("if", args=(condition, yes, no))
        if self.token in ("const", "let"):
            declaration = self.take()
            name = self.take()
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                raise QmlScriptError("expected a local variable name")
            self.take("=")
            return Stmt(declaration, name, (self.expression(),))
        expression = self.expression()
        if self.token in ("+=", "-="):
            operator = self.take()
            return Stmt("compound_assign", expression, (operator, self.expression()))
        if self.maybe("="):
            return Stmt("assign", expression, (self.expression(),))
        return Stmt("expr", args=(expression,))

    PRECEDENCE = {"||": 1, "&&": 2, "|": 3, "&": 4, "==": 5, "===": 5,
                  "!=": 5, "!==": 5, "<": 6, ">": 6, "<=": 6, ">=": 6, "+": 7, "-": 7}

    def expression(self, minimum=0):
        token = self.take()
        if token in ("!", "+", "-"):
            left = Expr("unary", token, (self.expression(8),))
        elif token == "(":
            left = self.expression()
            self.take(")")
        elif token.startswith(("'", '"')):
            try:
                left = Expr("literal", ast.literal_eval(token))
            except (SyntaxError, ValueError):
                raise QmlScriptError("invalid string literal") from None
            if not isinstance(left.value, str) or any(ord(char) < 32 or ord(char) > 126 for char in left.value):
                raise QmlScriptError("strings must contain printable ASCII")
        elif token.lower().startswith("0x") or token.isdigit() or re.fullmatch(r"(?:[0-9]+\.[0-9]*|\.[0-9]+)", token):
            left = Expr("literal", float(token) if "." in token else int(token, 0))
        elif token in ("true", "false"):
            left = Expr("literal", token == "true")
        elif re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token):
            left = Expr("name", token)
        else:
            raise QmlScriptError(f"unsupported expression token {token}")

        while True:
            if self.token == ".":
                self.take(".")
                field = self.take()
                if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", field):
                    raise QmlScriptError("expected a member name")
                left = Expr("member", field, (left,))
                continue
            if self.token == "(":
                self.take("(")
                args = []
                if self.token != ")":
                    while True:
                        args.append(self.expression())
                        if not self.maybe(","):
                            break
                self.take(")")
                left = Expr("call", None, (left, *args))
                continue
            if self.token == "?" and minimum <= 0:
                self.take("?")
                yes = self.expression()
                self.take(":")
                no = self.expression()
                left = Expr("conditional", None, (left, yes, no))
                continue
            precedence = self.PRECEDENCE.get(self.token, -1)
            if precedence < minimum:
                break
            operator = self.take()
            right = self.expression(precedence + 1)
            left = Expr("binary", operator, (left, right))
        return left

    def expression_source(self):
        value = self.expression()
        if self.token != "<eof>":
            raise QmlScriptError(f"unexpected token {self.token}")
        return value


@dataclass
class Value:
    c: str
    type: str


class Emitter:
    def __init__(self, name, current_node, nodes, slots, mouse_name,
                 coordinates=None,
                 geometry=None, locals_=None, checked_math=True):
        self.checked_math = checked_math
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise QmlScriptError("invalid generated handler name")
        self.name, self.current_node = name, current_node
        self.nodes, self.slots, self.mouse_name = nodes, slots, mouse_name
        self.lines, self.local_types, self.temp = [], {}, 0
        self.geometry = geometry or {}
        self.coordinates = coordinates or {}
        for key, value in (locals_ or {}).items():
            self.local_types[key] = value if isinstance(value, (dict, str)) else {
                "type": "int", "code": str(value)}

    def fail(self, message):
        raise QmlScriptError(message)

    def slot(self, path):
        if path not in self.slots:
            self.fail(f"unknown property {path}")
        spec = self.slots[path]
        if spec.get("type") not in ("string", "int", "bool"):
            self.fail(f"unsupported property type for {path}")
        return spec

    def node(self, name):
        if name not in self.nodes:
            self.fail(f"unknown object id {name}")
        return self.nodes[name]

    def name_value(self, name):
        if name in self.local_types:
            value = self.local_types[name]
            if isinstance(value, dict):
                return Value(value.get("code", name), value["type"])
            return Value(name, value)
        if name == self.mouse_name and self.mouse_name is not None:
            return Value("(*mouse)", "mouse")
        if name == "pressed":
            return Value(f"eqml_ui_pressed(ui, {self.current_node})", "bool")
        if name == "containsPress":
            return Value(f"eqml_ui_contains_press(ui, {self.current_node})", "bool")
        if name in self.slots:
            spec = self.slot(name)
            if spec["type"] == "string":
                return Value(f"eqml_ui_string(ui, {spec['index']})", "string")
            self.fail(f"reading {spec['type']} slots is not supported by the Runtime API")
        if name in ("true", "false"):
            return Value("1" if name == "true" else "0", "bool")
        self.fail(f"unknown identifier {name}")

    def expression(self, expr):
        op = expr.op
        if op == "literal":
            if isinstance(expr.value, bool):
                return Value("1" if expr.value else "0", "bool")
            if isinstance(expr.value, int):
                if not -(2**31) <= expr.value < 2**31:
                    self.fail("integer literal outside the signed 32-bit range")
                return Value(str(expr.value), "int")
            if isinstance(expr.value, float):
                return Value(repr(expr.value), "float")
            # C string escaping is deliberately independent of JavaScript escaping.
            import json
            return Value(json.dumps(expr.value).replace("?", r"\?"), "string")
        if op == "name":
            return self.name_value(expr.value)
        if op == "member":
            base = expr.args[0]
            if base.op == "name" and base.value == self.mouse_name and self.mouse_name is not None:
                fields = {"x": "int", "y": "int", "button": "int", "modifiers": "int",
                          "isClick": "bool", "wasHeld": "bool"}
                if expr.value not in fields:
                    self.fail(f"unsupported mouse property {expr.value}")
                cfield = {"isClick": "is_click", "wasHeld": "was_held"}.get(expr.value, expr.value)
                return Value(f"mouse->{cfield}", fields[expr.value])
            if base.op == "name" and base.value == "Qt":
                constants = {"LeftButton": "EQML_BUTTON_LEFT", "RightButton": "EQML_BUTTON_RIGHT",
                             "MiddleButton": "EQML_BUTTON_MIDDLE", "BackButton": "EQML_BUTTON_BACK",
                             "ForwardButton": "EQML_BUTTON_FORWARD", "TaskButton": "EQML_BUTTON_TASK",
                             "ShiftModifier": "EQML_MODIFIER_SHIFT"}
                if expr.value in constants:
                    return Value(constants[expr.value], "int")
                match = re.fullmatch(r"ExtraButton([4-9]|1[0-9]|2[0-4])", expr.value)
                if match:
                    return Value(f"EQML_BUTTON_EXTRA{match.group(1)}", "int")
                self.fail(f"unsupported Qt constant {expr.value}")
            if base.op == "name" and base.value in self.nodes and expr.value == "containsPress":
                return Value(f"eqml_ui_contains_press(ui, {self.node(base.value)})", "bool")
            if base.op == "name" and base.value in self.local_types and expr.value in ("x", "y"):
                local = self.name_value(base.value)
                if local.type == "point":
                    return Value(f"{local.c}.{expr.value}", "int")
            path = self.member_path(expr)
            if path in self.coordinates:
                spec = self.coordinates[path]
                return Value(f"eqml_ui_coordinate(ui, {spec['node']}, {spec['axis']})", "int")
            if path in self.geometry:
                value = self.geometry[path]
                if isinstance(value, dict):
                    return Value(str(value["code"]), value["type"])
                if isinstance(value, float):
                    return Value(repr(value), "float")
                return Value(str(value), "int")
            if path in self.slots:
                spec = self.slot(path)
                if spec["type"] == "string":
                    return Value(f"eqml_ui_string(ui, {spec['index']})", "string")
                if "visible_node" in spec:
                    return Value(f"eqml_ui_visible(ui, {spec['visible_node']})", "bool")
                if "code" in spec:
                    return Value(str(spec["code"]), spec["type"])
                self.fail(f"reading {spec['type']} slots is not supported by the Runtime API")
            self.fail(f"unsupported property reference {path}")
        if op == "unary":
            operand = expr.args[0]
            if expr.value == "-" and operand.op == "literal" and type(operand.value) is int:
                return self.expression(Expr("literal", -operand.value))
            value = self.expression(expr.args[0])
            if expr.value == "+" and value.type == "int":
                return value
            if expr.value == "!" and value.type == "bool":
                return Value(f"(!({value.c}))", "bool")
            if expr.value == "-" and value.type == "int":
                return self.integer_math(Value("0", "int"), "-", value)
            if expr.value in ("+", "-") and value.type == "float":
                return Value(f"({expr.value}({value.c}))", "float")
            self.fail("unsupported unary operation")
        if op == "binary" and expr.value == "+":
            return self.addition(expr)
        if op == "binary":
            left = self.expression(expr.args[0])
            branch_start = len(self.lines)
            right = self.expression(expr.args[1])
            operator = expr.value
            if operator in ("&&", "||") and len(self.lines) != branch_start:
                self.fail("logical operand requires unsupported lazy evaluation")
            if operator in ("+", "-") and left.type == right.type == "int":
                return self.integer_math(left, operator, right)
            if operator in ("+", "-") and left.type == right.type == "float":
                return Value(f"(({left.c}) {operator} ({right.c}))", "float")
            if operator in ("==", "===", "!=", "!==") and left.type == right.type:
                c_op = "==" if operator in ("==", "===") else "!="
                if left.type == "string":
                    return Value(f"(strcmp({left.c}, {right.c}) {c_op} 0)", "bool")
                return Value(f"(({left.c}) {c_op} ({right.c}))", "bool")
            if operator in ("<", "<=", ">", ">=") and left.type == right.type == "int":
                return Value(f"(({left.c}) {operator} ({right.c}))", "bool")
            if operator in ("&", "|") and left.type == right.type == "int":
                return Value(f"(({left.c}) {operator} ({right.c}))", "int")
            if operator in ("&&", "||") and left.type == right.type == "bool":
                return Value(f"(({left.c}) {operator} ({right.c}))", "bool")
            self.fail(f"unsupported typed operation {operator}")
        if op == "conditional":
            condition = self.expression(expr.args[0])
            branch_start = len(self.lines)
            yes = self.expression(expr.args[1])
            yes_lines = len(self.lines)
            no = self.expression(expr.args[2])
            if condition.type not in ("bool", "int") or yes.type != no.type:
                self.fail("conditional branches must have one common type")
            if len(self.lines) != yes_lines or yes_lines != branch_start:
                self.fail("conditional branch requires unsupported lazy evaluation")
            return Value(f"(({condition.c}) ? ({yes.c}) : ({no.c}))", yes.type)
        if op == "call":
            callee, *args = expr.args
            if callee.op == "member" and callee.value == "mapToItem" and len(args) == 3:
                from_name = callee.args[0]
                to_name, x, y = args
                if from_name.op != "name" or to_name.op != "name":
                    self.fail("mapToItem targets must be object ids")
                x_value, y_value = self.expression(x), self.expression(y)
                if x_value.type != "int" or y_value.type != "int":
                    self.fail("mapToItem coordinates must be integers")
                ident = self.temp
                self.temp += 1
                local = f"_eqml_point_{ident}"
                self.lines.append(f"\tconst struct eqml_point {local} = eqml_ui_map(ui, {self.node(from_name.value)}, {self.node(to_name.value)}, {x_value.c}, {y_value.c});")
                self.local_types[local] = "point"
                return Value(local, "point")
            if callee.op == "member" and callee.value in ("x", "y") and callee.args[0].op == "name":
                self.fail("point fields are properties, not calls")
            if callee.op == "name" and callee.value == "qsTr" and len(args) == 1:
                value = self.expression(args[0])
                if value.type != "string":
                    self.fail("qsTr argument must be a string")
                return value
            if callee.op == "member" and callee.value == "log" and self.member_path(callee) == "console.log" and len(args) == 1:
                value = self.expression(args[0])
                string = self.as_string(value)
                self.lines.append(f"\teqml_ui_log(ui, {string.c});")
                return Value("0", "int")
            self.fail("unsupported function call")
        self.fail(f"unsupported expression {op}")

    @staticmethod
    def member_path(expr):
        parts = []
        while expr.op == "member":
            parts.append(expr.value)
            expr = expr.args[0]
        if expr.op != "name":
            return "<expression>"
        return ".".join([expr.value, *reversed(parts)])

    def as_string(self, value):
        if value.type == "string":
            return value
        if value.type == "bool":
            return Value(f"(({value.c}) ? \"true\" : \"false\")", "string")
        if value.type == "float":
            name = f"_eqml_str_{self.temp}"
            self.temp += 1
            self.lines.append(f"\tchar {name}[32];")
            self.lines.append(f"\tif (snprintf({name}, sizeof({name}), \"%g\", (double)({value.c})) < 0) return -1;")
            return Value(name, "string")
        if value.type != "int":
            self.fail("only string, integer, and boolean values can be converted to text")
        name = f"_eqml_str_{self.temp}"
        self.temp += 1
        self.lines.append(f"\tchar {name}[32];")
        self.lines.append(f"\tif (snprintf({name}, sizeof({name}), \"%d\", (int)({value.c})) < 0) return -1;")
        return Value(name, "string")

    def integer_math(self, left, operator, right):
        if not self.checked_math:
            return Value(f"(({left.c}) {operator} ({right.c}))", "int")
        name = f"_eqml_wide_{self.temp}"
        self.temp += 1
        self.lines.append(f"\tint64_t {name} = (int64_t)({left.c}) {operator} (int64_t)({right.c});")
        self.lines.append(f"\tif ({name} < INT32_MIN || {name} > INT32_MAX) "
                          "{ errno = ERANGE; return -1; }")
        return Value(f"(int32_t){name}", "int")

    def addition(self, expr):
        # Preserve left-associative numeric addition until the first string.
        # Format a whole chain into one buffer on the embedded stack.
        tail = []
        while expr.op == "binary" and expr.value == "+":
            expr, term = expr.args
            tail.append(term)
        first = self.expression(expr)
        parts = []
        for term in reversed(tail):
            right = self.expression(term)
            if parts:
                parts.append(right)
            elif "string" in (first.type, right.type):
                parts = [first, right]
            elif first.type == right.type == "int":
                first = self.integer_math(first, "+", right)
            elif first.type == right.type == "float":
                first = Value(f"(({first.c}) + ({right.c}))", first.type)
            else:
                self.fail("unsupported typed operation +")
        if not parts:
            return first
        name = f"_eqml_concat_{self.temp}"
        length = f"_eqml_length_{self.temp}"
        self.temp += 1
        formats, arguments = [], []
        for part in parts:
            if part.type == "int":
                formats.append("%d")
                arguments.append(f"(int)({part.c})")
            else:
                formats.append("%s")
                arguments.append(self.as_string(part).c)
        self.lines.append(f"\tchar {name}[EQML_STRING_CAP];")
        self.lines.append(f'\tint {length} = snprintf({name}, sizeof({name}), "'
                          + "".join(formats) + '", ' + ", ".join(arguments) + ");")
        self.lines.append(f"\tif ({length} < 0 || (size_t){length} >= sizeof({name})) "
                          "{ errno = E2BIG; return -1; }")
        return Value(name, "string")

    def statement(self, statement, indent=1):
        prefix = "\t" * indent
        if statement.op == "block":
            outer = self.local_types.copy()
            self.lines.append(prefix + "{")
            for child in statement.args:
                self.statement(child, indent + 1)
            self.lines.append(prefix + "}")
            self.local_types = outer
        elif statement.op == "if":
            condition = self.expression(statement.args[0])
            if condition.type not in ("bool", "int"):
                self.fail("if condition must be boolean")
            self.lines.append(prefix + f"if ({condition.c}) {{")
            self.statement(statement.args[1], indent + 1)
            if statement.args[2] is not None:
                self.lines.append(prefix + "} else {")
                self.statement(statement.args[2], indent + 1)
            self.lines.append(prefix + "}")
        elif statement.op in ("let", "const"):
            if statement.value in self.nodes:
                self.fail(f"local variable shadows QML object id {statement.value}")
            value = self.expression(statement.args[0])
            ctype = {"int": "int32_t", "bool": "int", "string": "const char *", "point": "struct eqml_point"}.get(value.type)
            if ctype is None:
                self.fail(f"unsupported local type {value.type}")
            ident = f"_eqml_local_{self.temp}"
            self.temp += 1
            if value.type == "string":
                self.lines.append(prefix + f"char {ident}[EQML_STRING_CAP];")
                self.lines.append(prefix + f"if (eqml_string_copy({ident}, {value.c}) != 0) return -1;")
            else:
                self.lines.append(prefix + f"{ctype} {ident} = {value.c};")
                self.lines.append(prefix + f"(void){ident};")
            self.local_types[statement.value] = {"type": value.type, "code": ident,
                                                 "mutable": statement.op == "let"}
        elif statement.op == "assign":
            target, value = statement.value, self.expression(statement.args[0])
            if target.op == "member":
                path = self.member_path(target)
                if path in self.coordinates:
                    spec = self.coordinates[path]
                    if not spec.get("writable", True):
                        self.fail(spec.get("anchor_error", f"{path} is not writable"))
                    if value.type != "int":
                        self.fail(f"coordinate assignment requires an integer: {path}")
                    self.lines.append(prefix + f"if (eqml_ui_set_coordinate(ui, {spec['node']}, {spec['axis']}, {value.c}, {spec['duration_ms']}) != 0) return -1;")
                    return
                spec = self.slot(path)
                if "visible_node" in spec:
                    if value.type != "bool":
                        self.fail(f"visibility assignment requires a boolean: {path}")
                    self.lines.append(prefix + f"if (eqml_ui_set_visible(ui, {spec['visible_node']}, {value.c}) != 0) return -1;")
                    return
                if spec["type"] != value.type or spec["type"] != "string":
                    self.fail(f"assignment type does not match writable string slot {path}")
                self.lines.append(prefix + f"if (eqml_ui_set(ui, {spec['index']}, {value.c}) != 0) return -1;")
            elif target.op == "name" and target.value in self.local_types:
                spec = self.local_types[target.value]
                if not isinstance(spec, dict) or not spec.get("mutable"):
                    self.fail("assignment to a read-only local")
                if spec["type"] != value.type:
                    self.fail("local assignment changes its type")
                if value.type == "string":
                    self.lines.append(prefix + f"if (eqml_string_copy({spec['code']}, {value.c}) != 0) return -1;")
                else:
                    self.lines.append(prefix + f"{spec['code']} = {value.c};")
            elif target.op == "name" and target.value in self.slots:
                spec = self.slot(target.value)
                if spec["type"] != value.type or spec["type"] != "string":
                    self.fail(f"assignment type does not match writable string slot {target.value}")
                self.lines.append(prefix + f"if (eqml_ui_set(ui, {spec['index']}, {value.c}) != 0) return -1;")
            else:
                self.fail("assignment target must be a string property or local")
        elif statement.op == "compound_assign":
            target = statement.value
            if target.op != "member":
                self.fail("compound assignment target must be a coordinate property")
            path = self.member_path(target)
            if path not in self.coordinates:
                self.fail(f"unsupported compound assignment target {path}")
            spec = self.coordinates[path]
            if not spec.get("writable", True):
                self.fail(spec.get("anchor_error", f"{path} is not writable"))
            current = Value(f"eqml_ui_coordinate(ui, {spec['node']}, {spec['axis']})", "int")
            right = self.expression(statement.args[1])
            if right.type != "int":
                self.fail("coordinate arithmetic requires an integer")
            value = self.integer_math(current, statement.args[0][0], right)
            self.lines.append(prefix + f"if (eqml_ui_set_coordinate(ui, {spec['node']}, {spec['axis']}, {value.c}, {spec['duration_ms']}) != 0) return -1;")
        elif statement.op == "expr":
            value = self.expression(statement.args[0])
            self.lines.append(prefix + f"(void)({value.c});")
        else:
            self.fail(f"unsupported statement {statement.op}")

    def compile(self, tree):
        self.statement(tree)
        code = ["static int", f"{self.name}(struct eqml_ui *ui, size_t node, enum eqml_signal signal, const struct eqml_mouse *mouse)", "{"]
        code.append(f"\t(void)ui; (void)node; (void)signal; (void)mouse; /* current node {self.current_node} */")
        code.extend(self.lines)
        code.append("\treturn 0;")
        code.append("}")
        return "\n".join(code)


def compile_handler(source, name, *, current_node, nodes, slots, allow_mouse=True,
                    coordinates=None):
    tree, mouse_name = Parser(source).handler()
    if mouse_name is not None and not allow_mouse:
        raise QmlScriptError("this signal has no mouse event parameter")
    return Emitter(name, current_node, nodes, slots, mouse_name,
                   coordinates=coordinates).compile(tree)


@dataclass(frozen=True)
class CExpression:
    code: str
    type: str
    prelude: tuple


def compile_expression(source, *, nodes=None, slots=None, locals_=None, geometry=None):
    """Type and lower one side-effect-free binding expression to C."""
    tree = Parser(source).expression_source()
    emitter = Emitter("_eqml_expression", 0, nodes or {}, slots or {}, None,
                      geometry=geometry, locals_=locals_, checked_math=False)
    value = emitter.expression(tree)
    return CExpression(value.c, value.type, tuple(emitter.lines))


def emit_dispatch(name, signal_to_handler):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise QmlScriptError("invalid generated dispatcher name")
    grouped = {}
    for (node, signal), callback in signal_to_handler.items():
        if signal in SIGNALS:
            signal = signal_enum(signal)
        if signal not in SIGNALS.values():
            raise QmlScriptError(f"unsupported signal {signal}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", callback):
            raise QmlScriptError("invalid generated handler reference")
        grouped.setdefault(node, []).append((signal, callback))
    lines = ["static int", f"{name}(struct eqml_ui *ui, size_t node, enum eqml_signal signal, const struct eqml_mouse *mouse)", "{", "\tswitch (node) {"]
    for node, handlers in sorted(grouped.items()):
        lines.extend([f"\tcase {node}:", "\t\tswitch (signal) {"])
        for signal, callback in handlers:
            lines.append(f"\t\tcase {signal}: return {callback}(ui, node, signal, mouse);")
        lines.extend(["\t\tdefault: return 0;", "\t\t}"])
    lines.extend(["\tdefault: return 0;", "\t}", "}"])
    return "\n".join(lines)
