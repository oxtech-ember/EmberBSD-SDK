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
| Module and root | `import QtQuick` and one `Rectangle` with literal `width`, `height` (1–4096), optional `id` and opaque `#RRGGBB` background (default white) |
| Model | Root `property int name: constant`, signed 32-bit; bindings can reference it by name or `rootId.name` |
| Children | Zero or more `Text` items in paint order, literal RGB color (default black), printable ASCII text, up to 4096 bytes per literal part |
| String bindings | String literals and one root integer joined with `+`, e.g. `"T=" + gauge.value + " C"`; changing the integer takes effect on the next render |
| Geometry | Signed integer `x`, `y` (16-bit) or `anchors.horizontalCenter`, `anchors.verticalCenter`, `anchors.centerIn` relative to the parent |
| Font | Integer `font.pixelSize` from 7 to 64 (default 14); the runtime supplies its bitmap font |
| Syntax | Double-quoted strings with JSON escapes, line and block comments, newlines or semicolons between fields |

The compiler reports unsupported constructs with file, line and column. This
profile is an independently implemented subset. Full Qt Quick, Qt Quick
Ultralite and their platform toolchains remain separate application targets.
Measure the linked executable, resident memory and framebuffer separately.
