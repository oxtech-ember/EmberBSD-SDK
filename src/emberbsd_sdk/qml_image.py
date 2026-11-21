# SPDX-License-Identifier: BSD-2-Clause
"""Build-time PNG decoding for local, intrinsic-size QML images."""
from pathlib import Path
import struct
import subprocess


class BitmapAssets:
    def __init__(self, directory, decoder):
        self.directory = Path(directory).resolve()
        self.decoder = decoder
        self.bitmaps = {}
        self.lines = []
        self.pixels = 0

    def load(self, source):
        relative = Path(source)
        if (relative.is_absolute() or ".." in relative.parts or ":" in source or
                relative.suffix.lower() != ".png"):
            raise ValueError("Image source must be a local relative PNG path")
        path = (self.directory / relative).resolve(strict=True)
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError("Image source must be a file within the QML directory")
        if path in self.bitmaps:
            return self.bitmaps[path]
        if self.decoder is None or not Path(self.decoder).is_absolute():
            raise ValueError("Image requires --pngtopam with the absolute native Netpbm executable path")
        with path.open("rb") as stream:
            header = stream.read(33)
        if (len(header) != 33 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or
                header[24] != 8 or header[25] not in (2, 6)):
            raise ValueError(f"{source}: Image requires an 8-bit RGB or RGBA PNG")
        width, height = struct.unpack(">II", header[16:24])
        count = width * height
        if not (1 <= width <= 4096 and 1 <= height <= 4096) or self.pixels + count > 1048576:
            raise ValueError("Image assets exceed the scene budget (4096 per axis, 1048576 total pixels)")
        result = subprocess.run([self.decoder, "-alphapam", str(path)], capture_output=True)
        if result.returncode:
            raise ValueError(f"{source}: pngtopam failed: {result.stderr.decode(errors='replace').strip()}")
        header, end, data = result.stdout.partition(b"ENDHDR\n")
        fields = dict(line.split(maxsplit=1) for line in header.splitlines()[1:]
                      if line and not line.startswith(b"#"))
        if (not header.startswith(b"P7\n") or not end or fields.get(b"WIDTH") != str(width).encode() or
                fields.get(b"HEIGHT") != str(height).encode() or fields.get(b"DEPTH") != b"4" or
                fields.get(b"MAXVAL") != b"255" or fields.get(b"TUPLTYPE") != b"RGB_ALPHA" or
                len(data) != 4 * count):
            raise ValueError(f"{source}: unexpected RGB_ALPHA PAM output")
        symbol = f"bitmap_{len(self.bitmaps)}"
        rgb = [(r >> 3) << 11 | (g >> 2) << 5 | b >> 3 for r, g, b, _ in struct.iter_unpack("4B", data)]
        self.array("uint16_t", symbol + "_rgb", rgb, "04x")
        alpha = data[3::4]
        alpha_fields = ""
        if min(alpha) < 255:
            self.array("uint8_t", symbol + "_alpha", alpha, "02x")
            alpha_fields = f", .alpha = {symbol}_alpha, .alpha_count = {count}"
        self.lines.extend([f"static const struct eqml_bitmap {symbol} = {{",
                           f"    .width = {width}, .height = {height}, .pixels = {symbol}_rgb,",
                           f"    .pixel_count = {count}{alpha_fields}", "};", ""])
        self.pixels += count
        self.bitmaps[path] = (symbol, width, height)
        return self.bitmaps[path]

    def array(self, ctype, symbol, values, fmt):
        self.lines.append(f"static const {ctype} {symbol}[] = {{")
        for start in range(0, len(values), 16):
            self.lines.append("    " + ", ".join(f"0x{v:{fmt}}" for v in values[start:start+16]) + ",")
        self.lines.append("};")
