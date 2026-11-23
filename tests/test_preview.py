import base64
import io
import json
import struct
import sys
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pygmy import preview  # noqa: E402

TWO_TOOLS = b"""; header
M83
G90
T0
G1 Z0.2
G1 X10 Y10 F3000
G1 X50 Y10 E2
G1 X50 Y50 E2
G1 Z0.4
T1
G1 X10 Y50 E2
G1 X10 Y10 E2
G1 X30 Y10 E-1
"""


def pixels(png):
    """Decode our own PNG (filter 0 only) to a list of rows of (r, g, b, a)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        if png[pos + 4:pos + 8] == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = []
    for r in range(h):
        row = raw[r * (w * 4 + 1) + 1:(r + 1) * (w * 4 + 1)]
        rows.append([tuple(row[i:i + 4]) for i in range(0, len(row), 4)])
    return rows


def drawn(png):
    return [p for row in pixels(png) for p in row if p[3]]


class Render(unittest.TestCase):
    def test_a_valid_png_of_the_asked_size(self):
        out = preview.render(TWO_TOOLS, ["#ff0000", "#0000ff"], (300, 48))
        self.assertEqual(set(out), {300, 48})
        self.assertEqual(len(pixels(out[48])), 48)

    def test_each_tool_is_drawn_in_its_spool_colour(self):
        px = drawn(preview.render(TWO_TOOLS, ["#ff0000", "#0000ff"])[300])
        self.assertTrue(any(r > 100 and b < 10 for r, g, b, a in px), "red for T0")
        self.assertTrue(any(b > 100 and r < 10 for r, g, b, a in px), "blue for T1")

    def test_a_retraction_draws_nothing_and_nothing_extruded_gives_no_image(self):
        self.assertEqual(preview.render(b"G1 X10 Y10\nG1 X20 Y20 E-1\n"), {})

    def test_the_picture_is_not_mirrored(self):
        # G-code Y goes up: a path along high Y ends up in the top rows
        rows = pixels(preview.render(b"M83\nG1 X0 Y0\nG1 X100 Y100 E1\n", ["#ffffff"])[300])
        top = [x for x, p in enumerate(rows[20]) if p[3]]
        bottom = [x for x, p in enumerate(rows[280]) if p[3]]
        self.assertTrue(top and bottom)
        self.assertGreater(min(top), min(bottom) + 100)  # the diagonal goes from bottom-left to top-right


def sliced_3mf(with_png=False, colours=("#00ff00",)):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Metadata/plate_1.gcode", TWO_TOOLS)
        z.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": list(colours)}))
        if with_png:
            z.writestr("Metadata/plate_1.png", b"original")
    return out.getvalue()


class Archive(unittest.TestCase):
    def test_plates_without_an_image_get_one_in_the_spool_colours(self):
        data = preview.with_previews(sliced_3mf(), ["#ff0000", "#0000ff"])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("Metadata/plate_1_small.png", z.namelist())
            self.assertTrue(any(r > 100 and b < 10 for r, g, b, a in drawn(z.read("Metadata/plate_1.png"))))

    def test_an_existing_image_is_left_alone(self):
        data = sliced_3mf(with_png=True)
        self.assertEqual(preview.with_previews(data), data)

    def test_without_given_colours_the_ones_in_the_3mf_are_used(self):
        data = preview.with_previews(sliced_3mf(colours=("#00ff00",)))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertTrue(any(g > 100 and r < 10 for r, g, b, a in drawn(z.read("Metadata/plate_1.png"))))

    def test_not_a_zip_comes_back_unchanged(self):
        self.assertEqual(preview.with_previews(b"not a zip"), b"not a zip")


class Klipper(unittest.TestCase):
    def test_the_block_is_the_standard_thumbnail_header(self):
        pngs = preview.render(TWO_TOOLS, ["#ff0000"], (300, 48))
        text = preview.klipper_gcode(TWO_TOOLS, pngs).decode()
        self.assertTrue(text.startswith("; THUMBNAIL_BLOCK_START\n; thumbnail begin 300x300 "))
        block = text.split("; THUMBNAIL_BLOCK_END")[0]
        self.assertIn("; thumbnail begin 48x48 ", block)
        body, size = [], None
        for line in block.splitlines():
            if line.startswith("; thumbnail begin 300x300"):
                size, body = int(line.split()[-1]), []
            elif line.startswith("; thumbnail end"):
                break
            elif size is not None:
                self.assertLessEqual(len(line), 80)
                body.append(line[2:])
        self.assertEqual(len("".join(body)), size)
        self.assertEqual(base64.b64decode("".join(body)), pngs[300])
        self.assertTrue(text.endswith(TWO_TOOLS.decode()))

    def test_a_gcode_that_already_has_a_thumbnail_is_not_touched(self):
        data = b"; thumbnail begin 300x300 4\n; AAAA\n; thumbnail end\n" + TWO_TOOLS
        self.assertEqual(preview.klipper_gcode(data, {300: b"x"}), data)


if __name__ == "__main__":
    unittest.main()
