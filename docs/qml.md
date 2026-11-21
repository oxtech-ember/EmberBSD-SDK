# EmberBSD QML

The SDK compiles a small, explicit QML profile to C99 scene data. The target
uses the [EmberBSD Runtime renderer](https://github.com/oxtech-ember/EmberBSD-Runtime/tree/main/qml)
and has no QML parser, JavaScript engine or Qt library dependency. Static
scenes use application-owned model values. Interactive scenes carry a bounded
node tree and compiled C callbacks for the supported handlers.

From the SDK checkout, run the generator with the native SDK Python:

```sh
"$HOST_PREFIX/bin/python3.15" scripts/qml-compile.py Weather.qml -o weather-scene.c
```

Compile that file for the target with the renderer's `qml/` include directory.
It exports `const struct eqml_scene eqml_app`; `--name dashboard` exports
`eqml_dashboard` instead. Generated C checks scene ABI 5 at compile time. The
[weather example](https://github.com/oxtech-ember/EmberBSD-Examples/tree/main/desktop/mosaico-weather)
exercises cross-linking, model updates and the RGB565 wsdisplay backend.

## Static profile

| Construct | Accepted values |
| --- | --- |
| Module and root | `import QtQuick` or `import QtQuick 2.0` and one `Rectangle` or `Item` with literal `width`, `height` (1–4096). `Item` uses the renderer's white default background. Other versioned module imports are rejected. |
| Model | Root `property int name: constant`, signed 32-bit. Text bindings can reference integer properties by name or `rootId.name`. A root boolean literal may select a static layout branch; it is evaluated at compile time and is not exported as a mutable scene property. |
| Children | `Rectangle`, `Item`, `Text`, `Image`, `Grid` and `Row` items. A nested rectangle takes literal `width` and `height` (0–4096, default zero) and can contain children. An optional `id` must be unique within the scene. |
| Grid | Static left-to-right layout of fixed-size rectangles. Optional integer `columns` (1–4096, default 4), `spacing` (0–4096, default 0), and parent-centering anchors. Row and column sizes follow their largest cells. The compiler computes positions on the host; the grid's implicit dimensions must fit within 4096 × 4096. |
| Row | Static horizontal layout of fixed-size rectangles. Integer `spacing` (0–4096, default 0), optional parent-centering anchors, and `layoutDirection` as `Qt.LeftToRight`, `Qt.RightToLeft`, or the supported root-boolean ternary form. The compiler evaluates the boolean literal; it is not a mutable runtime property and no live QML binding is kept. Zero-sized children do not participate. Visible children retain their own sizes and are top-aligned. The implicit dimensions must fit within 4096 × 4096. |
| Colors | Opaque `#RRGGBB` or one of the 16 basic SVG color names, case insensitive. Rectangles default to white and text to black. |
| Paint order | Integer `z` sorts siblings (signed 32-bit, default zero); ties keep declaration order. A negative-z child paints before its parent. Each sibling paints its whole subtree; children can extend outside their parent. The scene bounds clip the final image. |
| String bindings | Printable ASCII literals (up to 4096 bytes per literal part) and one root integer joined with `+`, e.g. `"T=" + gauge.value + " C"`; changing the integer takes effect on the next render |
| Geometry | Signed integer `x`, `y` offsets (16-bit) relative to the parent. Grid and Row determine the offsets of their children. Text, Grid and Row support `anchors.horizontalCenter`, `anchors.verticalCenter`, or `anchors.centerIn` relative to their immediate parent. |
| Font | Integer `font.pixelSize` from 7 to 64 (default 14); the runtime supplies its bitmap font. |
| Syntax | Double-quoted strings with JSON escapes, line and block comments, newlines or semicolons between fields |

## Interactive profile

An interactive scene uses a fixed-size `Rectangle` or `Item` root with nested
`Rectangle`, `Item`, `Text`, `Image` and `MouseArea` nodes. An `Item` positions its
children without drawing a background. The viewport clears to white. The compiler emits nodes in
parent-first order and sorts nonnegative sibling `z` values stably; negative
`z` and `Grid` mixed with interactive nodes are rejected. The Runtime resolves
supported parent-relative anchors and bottom-to-top/bottom dependencies;
layout cycles are rejected during compilation. A scene may contain at most 256
nodes and 256 string slots; each initial interactive `Text` label is limited to
255 printable bytes. `MouseArea` supports
`anchors.fill: parent`, `acceptedButtons` (default left button or
`Qt.AllButtons`), `hoverEnabled`, and `Drag` to a `Rectangle`. Drag defaults to
both axes; each enabled axis requires constant minimum and maximum bounds.

`MouseArea` handlers support `onEntered`, `onExited`, `onPressed`,
`onReleased`, `onClicked`, `onDoubleClicked` and `onPressAndHold`; `Text`
supports `onTextChanged`. `onEntered`, `onExited` and `onTextChanged` do not
take a mouse event argument. Handler bodies are compiled to C from a bounded
JavaScript subset (expressions, assignments, local declarations and `if`).
Local declarations may not shadow QML object ids.
Mouse input fields, supported Qt button/modifier constants, text slots,
`containsPress`, and `mapToItem` are type-checked by the host compiler. Text
labels may be literal strings; mutable labels use stable Runtime string slots.
Handlers may read and assign `id.visible` as a boolean. Initial visibility
defaults to true and accepts a boolean literal. Hiding a parent also hides its
children. Interactive rectangles accept literal opacity from zero through one
and the existing `containsPress` binding. A transparent visible item can still
receive pointer events.

Identified `Rectangle` nodes also support `x` and `y` reads and assignments in
handlers. `+=` uses checked signed 32-bit arithmetic. An axis with no anchor
updates immediately; `Behavior on x` or `Behavior on y` accepts only
`NumberAnimation { duration: <integer> }` and animates assignments to that
axis. Coordinate writes to an anchored axis and behaviors on anchored axes are
rejected. Other animation properties and animation types are outside this
profile.
Unsupported QML or JavaScript constructs fail with a source location instead
of being emitted as target code.

The compiler reports unsupported constructs with file, line and column. This
profile is an independently implemented subset. Full Qt Quick, Qt Quick
Ultralite and their platform toolchains remain separate application targets.
Measure the linked executable, resident memory and framebuffer separately.

Regenerate scene C from the original QML after changing the scene ABI. Static
scenes emit a flat list in final paint order, allowing RGB565 scanlines to be
rendered without a scene-tree walk. Interactive scenes retain the bounded node
tree needed for hit-testing, anchors and input dispatch.

## Local PNG images

A nested `Image` loads a double-quoted local `source` relative to the QML
file. Supply `--pngtopam "$HOST_PREFIX/bin/pngtopam"` when compiling images.
The host compiler decodes 8-bit RGB and RGBA PNGs once per distinct file,
embeds RGB565 pixels and includes an 8-bit alpha plane when needed. A scene
can reference up to 1,048,576 distinct image pixels, with up to 4,096 per
axis. Each image uses its intrinsic dimensions and explicit x/y placement.
Interactive handlers can switch its visibility and compare integer values
with `<`, `<=`, `>`, and `>=`. The target reads the linked data directly.
