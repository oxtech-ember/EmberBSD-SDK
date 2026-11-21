# EmberBSD QML

The SDK compiles a small, explicit QML profile to static C99 scene data.
The target uses the [EmberBSD Runtime renderer](https://github.com/oxtech-ember/EmberBSD-Runtime/tree/main/qml)
and an application-owned array of 32-bit property values. It has no QML parser,
JavaScript engine or Qt library dependency on the target. Each application
instance can keep its own values and render again after a change.

From the SDK checkout, run the generator with the native SDK Python:

```sh
"$HOST_PREFIX/bin/python3.15" scripts/qml-compile.py Weather.qml -o weather-scene.c
```

Compile that file for the target with the renderer's `qml/` include directory.
It exports `const struct eqml_scene eqml_app`; `--name dashboard` exports
`eqml_dashboard` instead. The generated translation unit checks the renderer
ABI at compile time. The [weather example](https://github.com/oxtech-ember/EmberBSD-Examples/tree/main/desktop/mosaico-weather)
exercises cross-linking, model updates and the RGB565 wsdisplay backend.

## Source profile

| Construct | Accepted values |
| --- | --- |
| Module and root | `import QtQuick` and one opaque `Rectangle` with literal `width`, `height` (1–4096) |
| Model | Root `property int name: constant`, signed 32-bit; bindings can reference it by name or `rootId.name` |
| Children | `Rectangle`, `Text` and `Grid` items. A nested rectangle takes literal `width` and `height` (0–4096, default zero) and can contain children. The profile accepts up to 64 nested containers. An optional `id` must be unique within the scene. |
| Grid | Static left-to-right layout of fixed-size rectangles. Optional integer `columns` (1–4096, default 4), `spacing` (0–4096, default 0), and parent-centering anchors. Row and column sizes follow their largest cells. The compiler computes positions on the host; the grid's implicit dimensions must fit within 4096 × 4096. |
| Colors | Opaque `#RRGGBB` or one of the 16 basic SVG color names, case insensitive. Rectangles default to white and text to black. |
| Paint order | Integer `z` sorts siblings (signed 32-bit, default zero); ties keep declaration order. A negative-z child paints before its parent. Each sibling paints its whole subtree; children can extend outside their parent. The scene bounds clip the final image. |
| String bindings | Printable ASCII literals (up to 4096 bytes per literal part) and one root integer joined with `+`, e.g. `"T=" + gauge.value + " C"`; changing the integer takes effect on the next render |
| Geometry | Signed integer `x`, `y` offsets (16-bit) relative to the parent. A grid determines the offsets of its cells. Text and Grid support `anchors.horizontalCenter`, `anchors.verticalCenter`, or `anchors.centerIn` relative to their immediate parent. |
| Font | Integer `font.pixelSize` from 7 to 64 (default 14); the runtime supplies its bitmap font |
| Syntax | Double-quoted strings with JSON escapes, line and block comments, newlines or semicolons between fields |

The compiler reports unsupported constructs with file, line and column. This
profile is an independently implemented subset. Full Qt Quick, Qt Quick
Ultralite and their platform toolchains remain separate application targets.
Measure the linked executable, resident memory and framebuffer separately.

Regenerate scene C from the original QML when updating to scene ABI 2.
The compiler emits a flat list of rectangles and text in final paint order.
The renderer can therefore draw each RGB565 scanline without traversing a
scene tree on the target.
