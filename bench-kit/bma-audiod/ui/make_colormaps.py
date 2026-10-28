#!/usr/bin/env python3
"""Generates the 256-entry colour tables embedded in static/spectrum.js (viridis and magma: CC0; turbo: Apache-2.0, Google; grey).
    python make_colormaps.py > colormaps.txt      (needs matplotlib; prints the `const CMAP_HEX = {...}` block)
Each table is 768 bytes as 1536 hex digits (r, g, b per entry), index 0 = lowest level."""

import matplotlib

print("const CMAP_HEX = {")
for name in ("viridis", "magma", "turbo"):
    cm = matplotlib.colormaps[name]
    hx = "".join("%02x%02x%02x" % tuple(int(round(c * 255)) for c in cm(i / 255.0)[:3]) for i in range(256))
    print('  %s: "%s",' % (name, hx))
print("};")
