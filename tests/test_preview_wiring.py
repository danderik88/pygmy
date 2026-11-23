import base64
import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pygmy import server  # noqa: E402

GCODE = b"M83\n;LAYER_CHANGE\nG1 Z0.2\nG1 X10 Y10\nG1 X50 Y10 E2\nG1 X50 Y50 E2\n"


def sliced(png=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Metadata/plate_1.gcode", GCODE)
        if png:
            z.writestr("Metadata/plate_1.png", png)
    return out.getvalue()


class Wiring(unittest.TestCase):
    def test_a_plate_without_an_image_is_drawn_for_the_app(self):
        out = server.plate_png(sliced(), "", any_plate=True)
        self.assertTrue(base64.b64decode(out).startswith(b"\x89PNG"))

    def test_a_stored_image_wins_over_drawing(self):
        out = server.plate_png(sliced(b"stored"), "", any_plate=True)
        self.assertEqual(base64.b64decode(out), b"stored")

    def test_a_project_that_is_not_sliced_has_no_drawn_image(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("3D/3dmodel.model", "<model/>")
        self.assertIsNone(server.plate_png(out.getvalue(), "", any_plate=True))

    def test_the_klipper_upload_carries_the_thumbnail_block(self):
        link = mock.Mock()
        link.start_print.return_value = True
        server.start_klipper(link, "cube.gcode.3mf", sliced())
        name, data = link.upload.call_args[0]
        self.assertEqual(name, "cube.gcode")
        self.assertTrue(data.startswith(b"; THUMBNAIL_BLOCK_START\n; thumbnail begin 300x300 "))
        self.assertTrue(data.endswith(GCODE))


if __name__ == "__main__":
    unittest.main()
