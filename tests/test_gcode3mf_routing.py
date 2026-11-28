"""A .gcode.3mf is already sliced: it goes to the Files list, never to the Print now slicer. Run: python3 -m unittest discover -s tests"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pygmy import server  # noqa: E402

PAGE = (ROOT / "src" / "pygmy" / "app.html").read_text()


class ServerRefusal(unittest.TestCase):
    def setUp(self):
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True)

    def test_print_now_refuses_a_sliced_file(self):
        for name in ("Box.gcode.3mf", "Box.GCODE.3MF"):
            with self.assertRaises(server.ToolError) as ctx:
                server.upload_model("a1", name, b"pk", draft=True)
            self.assertEqual(str(ctx.exception), "This file is already sliced: it goes to Files, not to Print now.")
        with self.assertRaises(server.ToolError):
            server.upload_model("a1", "Box.gcode.3mf", b"pk")


class AppRouting(unittest.TestCase):
    def test_print_now_upload_hands_a_sliced_file_to_files(self):
        handler = PAGE[PAGE.index('$("mfile").onchange'):]
        handler = handler[:handler.index('$("mwsend").onclick')]
        self.assertRegex(handler, r"/\\\.gcode\\\.3mf\$/i\.test\(f\.name\)")
        self.assertIn("/upload/", handler)
        self.assertIn("Sliced file added:", handler)
        self.assertIn("showFiles()", handler)


if __name__ == "__main__":
    unittest.main()
