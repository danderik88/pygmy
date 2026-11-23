"""Plate preview drawn from sliced G-code: a top-down view of the extruded paths in the spool colours.

OrcaSlicer runs headless on the server and writes no thumbnail, so Pygmy draws its own. Stdlib only.
"""
import base64
import io
import json
import re
import struct
import zipfile
import zlib
from array import array

DEFAULT_COLOR = "#1fb5a5"
_AXES = re.compile(rb"([XYZE])(-?\d*\.?\d+)")
_TOOL = re.compile(rb"^T(\d{1,2})\b")  # T255 / T1000 are not filaments
_PLATE_GCODE = re.compile(r"Metadata/plate_(\d+)\.gcode")
LEVELS = 16  # brightness steps from the first layer (dark) to the last (light)


def _scene(gcode):
    """Every extruding move as (x0, y0, x1, y1, z, tool) in flat arrays. G2/G3 arcs are not drawn."""
    xs = array("f")
    tools = bytearray()
    x = y = z = e = 0.0
    tool = 0
    rel_xyz = rel_e = False
    layer_marks = (b";LAYER_CHANGE", b"; layer num/total_layer_count")  # Klipper / Bambu flavour of Orca
    gated = any(mark in gcode for mark in layer_marks)  # what comes before the first layer is the prime line
    live = not gated
    for raw in gcode.split(b"\n"):
        if gated and not live and raw.startswith(layer_marks):
            live = True
        line = raw.split(b";", 1)[0].strip()
        if not line:
            continue
        head = line[:3]
        if line[:2] in (b"G0", b"G1") and line[2:3] in (b" ", b"\t", b""):
            v = {k: float(n) for k, n in _AXES.findall(line)}
            nx = x + v[b"X"] if rel_xyz and b"X" in v else v.get(b"X", x)
            ny = y + v[b"Y"] if rel_xyz and b"Y" in v else v.get(b"Y", y)
            nz = z + v[b"Z"] if rel_xyz and b"Z" in v else v.get(b"Z", z)
            if b"E" in v:
                de = v[b"E"] if rel_e else v[b"E"] - e
                e = e + v[b"E"] if rel_e else v[b"E"]
            else:
                de = 0.0
            if live and de > 0 and (nx != x or ny != y):
                xs.extend((x, y, nx, ny, nz))
                tools.append(tool)
            x, y, z = nx, ny, nz
        elif head == b"G92":
            v = {k: float(n) for k, n in _AXES.findall(line)}
            e = v.get(b"E", e)
        elif line == b"G90":
            rel_xyz = False
        elif line == b"G91":
            rel_xyz = True
        elif line == b"M82":
            rel_e = False
        elif line == b"M83":
            rel_e = True
        elif (m := _TOOL.match(line)):
            tool = int(m.group(1))
    return xs, tools


def _rgb(color):
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?", color or "")
    h = m.group(1) if m else DEFAULT_COLOR[1:]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _png(w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[r * w * 4:(r + 1) * w * 4]) for r in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _draw(scene, colors, size):
    xs, tools = scene
    n = len(tools)
    if not n:
        return None
    px = xs[0::5] + xs[2::5]
    py = xs[1::5] + xs[3::5]
    x0, x1, y0, y1 = min(px), max(px), min(py), max(py)
    zs = xs[4::5]
    z0, z1 = min(zs), max(zs)
    margin = size * 0.06
    scale = (size - 2 * margin) / max(x1 - x0, y1 - y0, 1e-3)
    ox = (size - (x1 - x0) * scale) / 2
    oy = (size - (y1 - y0) * scale) / 2
    brush = max(1, round(0.5 * scale))  # about one line width
    palette = {}
    buf = bytearray(size * size * 4)

    def paint(cx, cy, rgba):
        for dy in range(brush):
            yy = cy + dy
            if 0 <= yy < size:
                for dx in range(brush):
                    xx = cx + dx
                    if 0 <= xx < size:
                        i = (yy * size + xx) * 4
                        buf[i:i + 4] = rgba

    for k in range(n):
        j = k * 5
        level = round((xs[j + 4] - z0) / (z1 - z0) * (LEVELS - 1)) if z1 > z0 else LEVELS - 1
        key = (tools[k], level)
        rgba = palette.get(key)
        if rgba is None:
            r, g, b = _rgb(colors[tools[k]] if tools[k] < len(colors) else colors[0] if colors else DEFAULT_COLOR)
            f = 0.55 + 0.45 * level / (LEVELS - 1)
            rgba = palette[key] = bytes((int(r * f), int(g * f), int(b * f), 255))
        ax = int(ox + (xs[j] - x0) * scale)
        ay = int(size - oy - (xs[j + 1] - y0) * scale)  # G-code Y goes up, the picture goes down
        bx = int(ox + (xs[j + 2] - x0) * scale)
        by = int(size - oy - (xs[j + 3] - y0) * scale)
        dx, dy = abs(bx - ax), -abs(by - ay)
        sx, sy = (1 if ax < bx else -1), (1 if ay < by else -1)
        err = dx + dy
        while True:
            paint(ax, ay, rgba)
            if ax == bx and ay == by:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                ax += sx
            if e2 <= dx:
                err += dx
                ay += sy
    return _png(size, size, buf)


def render(gcode, colors=(), sizes=(300,)):
    """{size: PNG bytes} of the plate drawn from its G-code, or {} when nothing is extruded."""
    scene = _scene(gcode)
    pngs = {s: _draw(scene, list(colors), s) for s in sizes}
    return {s: p for s, p in pngs.items() if p}


def archive_colors(data):
    """Filament colours of a sliced 3MF, in filament order."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            found = json.loads(z.read("Metadata/project_settings.config")).get("filament_colour")
    except (KeyError, ValueError, zipfile.BadZipFile):
        return []
    return [c for c in found if isinstance(c, str)] if isinstance(found, list) else []


def with_previews(data, colors=()):
    """The sliced 3MF with plate_N.png (300) and plate_N_small.png (48) for every plate that has none."""
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return data
    with src:
        names = set(src.namelist())
        missing = [n for n in sorted(names) if (m := _PLATE_GCODE.fullmatch(n)) and f"Metadata/plate_{m.group(1)}.png" not in names]
        if not missing:
            return data
        colors = list(colors) or archive_colors(data)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            for n in missing:
                num = _PLATE_GCODE.fullmatch(n).group(1)
                pngs = render(src.read(n), colors, (300, 48))
                if 300 in pngs:
                    dst.writestr(f"Metadata/plate_{num}.png", pngs[300])
                if 48 in pngs:
                    dst.writestr(f"Metadata/plate_{num}_small.png", pngs[48])
    return out.getvalue()


def plate_images(data, plate):
    """{size: PNG} of one plate of a sliced 3MF: its square stored images, else drawn now (files sliced before this existed)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        found = {}
        for n in (f"Metadata/plate_{plate}.png", f"Metadata/plate_{plate}_small.png"):
            png = z.read(n) if n in names else b""
            if png[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", png[16:24])
                if w == h:
                    found[w] = png
        if found or f"Metadata/plate_{plate}.gcode" not in names:
            return found
        return render(z.read(f"Metadata/plate_{plate}.gcode"), archive_colors(data), (300, 48))


def thumbnail_block(pngs):
    """The thumbnail header Moonraker, Fluidd and KlipperScreen read, from {size: PNG bytes}."""
    lines = ["; THUMBNAIL_BLOCK_START"]
    for size, png in sorted(pngs.items(), reverse=True):
        text = base64.b64encode(png).decode()
        lines.append(f"; thumbnail begin {size}x{size} {len(text)}")
        lines += ["; " + text[i:i + 78] for i in range(0, len(text), 78)]
        lines.append("; thumbnail end")
    lines.append("; THUMBNAIL_BLOCK_END")
    return ("\n".join(lines) + "\n").encode()


def klipper_gcode(data, pngs):
    """Plate G-code with the thumbnail block on top, unless Orca already wrote one."""
    if not pngs or b"thumbnail begin" in data[:200_000]:
        return data
    return thumbnail_block(pngs) + data
