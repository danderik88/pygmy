"""Task 7: Print now Standard/Custom sheet, MakerWorld code popup, upload query on the server."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pygmy import server  # noqa: E402

PAGE = (ROOT / "src" / "pygmy" / "app.html").read_text()
TOKEN = "t" * 40


class ModelPut(unittest.TestCase):
    def test_put_forwards_engine_slot_choices(self):
        server.CFG.update(printers={"a1": 1}, tokens={"iphone": TOKEN}, control=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        seen = {}

        def fake_upload(alias, name, data, engine=None, choices=None, slot=None, draft=False):
            seen.update(alias=alias, name=name, engine=engine, choices=choices, slot=slot)
            return {"request_id": 1}

        choices = {"supports": "auto", "walls": 3, "reorient": True}
        url = (f"http://127.0.0.1:{httpd.server_address[1]}/model/a1/part.3mf"
               f"?engine=server&slot=A1&choices={urllib.parse.quote(json.dumps(choices))}")
        req = urllib.request.Request(url, data=b"mesh", method="PUT")
        req.add_header("Authorization", "Bearer " + TOKEN)
        with mock.patch.object(server, "upload_model", fake_upload):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        self.assertEqual(seen, {"alias": "a1", "name": "part.3mf", "engine": "server",
                                "choices": choices, "slot": "A1"})

    def test_no_query_keeps_the_plain_upload(self):
        self.assertEqual(server.parse_model_put("/model/a1/part.3mf"), ("a1", "part.3mf", None, None, None))

    def test_choices_must_be_a_json_object(self):
        for bad in ('["x"]', "nope", '"text"'):
            with self.assertRaises(server.ToolError):
                server.parse_model_put("/model/a1/part.3mf?choices=" + urllib.parse.quote(bad))


class PrinterInfoOrcaCad(unittest.TestCase):
    STATUS = {"connected": True, "state": "FINISH", "hms_errors": [], "ams": [], "tray_now": 255,
              "nozzles": [{}], "vt_tray": []}

    def info(self, cfg):
        with mock.patch.object(server, "read_status", lambda alias: self.STATUS), \
                mock.patch.object(server, "load_profile", lambda alias: {"model": "A1"}), \
                mock.patch.object(server.slicer, "available", lambda: False), \
                mock.patch.object(server, "DB", None), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1}, **cfg}):
            return json.loads(server.tool_printer_info({"printer": "a1"})[0]["text"])

    def test_printer_info_orca_cad_flag(self):
        self.assertFalse(self.info({"orca_wake_url": None})["orca_cad"])
        self.assertTrue(self.info({"orca_wake_url": "http://orca-cad/wake"})["orca_cad"])


class AppPage(unittest.TestCase):
    def test_print_settings_needles(self):
        for text in ("Customize", "Set this spool in Filament first", "Send to Orca-Cad", "Re-orient",
                     "MakerWorld code", 'id="pspool"', 'id="pcustomrows"', 'id="mwlogin"'):
            self.assertIn(text, PAGE)
        for text in ("Print settings", "Print now", "3MF files keep the designer's orientation",
                     "MakerWorld needs a code sent to your email.", "Signed in as", "Not signed in"):
            self.assertIn(text, PAGE)

    def test_no_maker_name_in_new_text(self):
        for pattern in (r'<div class="card" id="psettings".*?</section>',
                        r'<div class="sheet" id="mwlogin".*?</div></div>'):
            block = re.search(pattern, PAGE, re.S).group(0)
            for word in ("Bambu", "BBL"):
                self.assertNotIn(word, block)

    def test_too_big_note_is_shown_as_is(self):
        en_table = PAGE.split("const EN = [", 1)[1].split("];", 1)[0]
        self.assertNotIn("Too big for this printer", en_table)
        self.assertIn("const enNote", PAGE)

    def test_en_table_print_now_fixes(self):
        en_table = PAGE.split("const EN = [", 1)[1].split("];", 1)[0]
        self.assertIn(r"/copies: da 1 a 20/", en_table)
        self.assertIn("Copies: 1 to 20", en_table)
        self.assertIn(r"/Profilo di .* mancante/", en_table)
        self.assertIn("This printer's profile is missing", en_table)

    def test_app_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH")
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", PAGE, re.S)
        self.assertTrue(blocks)
        for i, code in enumerate(blocks):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"app{i}.js"
                path.write_text(code)
                run = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()


class SeveralPlates(unittest.TestCase):
    """A request that came out as several plates lists them all (2026-11-23: only the first one showed)."""
    def test_the_app_lists_every_plate_with_print_queue_and_add_all(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "pygmy" / "app.html").read_text()
        for needle in ("pnReadyMany", "paintPlates", 'data-pp="', 'data-pq="', 'data-pall="1"', 'id="pnplates"'):
            self.assertIn(needle, html)
        self.assertIn("(r.plates || []).length > 1", html)
        self.assertIn("pnStartPrint(id, null, printer, PN.plateIds.filter", html)  # printing one queues the rest
