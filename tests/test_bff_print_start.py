"""Upload and two-step print start against a fake printer link. Run: python3 -m unittest discover -s tests"""
import base64
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pygmy import server  # noqa: E402

JPEG = b"\xff\xd8fake-jpeg"
AUTH = "tok-" + "x" * 40


def make_3mf(model="Bambu Lab A1", nozzle="0.4", filaments=("PLA",), plates=1, colors=None):
    buf = io.BytesIO()
    colors = tuple(colors or (None,) * len(filaments))
    with zipfile.ZipFile(buf, "w") as z:
        for n in range(1, plates + 1):
            z.writestr(f"Metadata/plate_{n}.gcode", "G28\n")
        z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": model, "nozzle_diameter": [nozzle]}))
        rows = "".join(f'<filament id="{i}" type="{t}"' + (f' color="{c}"' if c else "") + ' used_g="9"/>'
                       for i, (t, c) in enumerate(zip(filaments, colors), 1))
        z.writestr("Metadata/slice_info.config", f'<config><plate><metadata key="prediction" value="1405"/><metadata key="weight" value="9.15"/>{rows}</plate></config>')
    return buf.getvalue()


class FakeFeed:
    def latest(self, **_):
        return JPEG


PROFILE = {
    "model": "A1",
    "slicer_model": "Bambu Lab A1",
    "command_timeout_s": 0.2,
    "start_timeout_s": 0.4,
    "supports": {"pause": True, "resume": True, "stop": True, "start": True},
    "limits": {},
}


def payload(content):
    return next(json.loads(c["text"]) for c in content if c["type"] == "text")


class PrintStartTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        server.CFG.pop("control", None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={"imac": AUTH}, control=True,
                          profiles=str(tmp), public_url="http://bff.test:8765")
        server.open_db(str(tmp / "state.db"))
        server.library.setup(str(tmp / "files"))
        server.PHOTOS.clear()
        self.state = {
            "connected": True, "state": "FAILED", "hms_errors": [], "ams": [],
            "vt_tray": [{"id": 254, "tray_type": "PLA"}], "nozzles": [{"nozzle_diameter": "0.4"}],
        }
        self.link = mock.Mock(host="h", access_code="c", fingerprint_dir=self.tmp.name)
        self.link.snapshot.side_effect = lambda: json.loads(json.dumps(self.state))
        self.link.start_print.side_effect = self.started
        link_patch = mock.patch.object(server, "link_of", return_value=self.link)
        link_patch.start()
        self.addCleanup(link_patch.stop)
        upload_patch = mock.patch.object(server.ftps, "upload")
        self.upload_ftp = upload_patch.start()
        self.addCleanup(upload_patch.stop)
        server.FEEDS["a1"] = FakeFeed()
        self.addCleanup(server.FEEDS.clear)

    def started(self, *_):
        self.state["state"] = "PREPARE"
        return True

    def tearDown(self):
        server.DB.close()
        self.tmp.cleanup()

    def upload(self, **kwargs):
        return server.upload_file("cubo.gcode.3mf", make_3mf(**kwargs))

    def prepare(self, file_id):
        return server.tool_prepare_print_start({"printer": "a1", "file_id": file_id})

    def confirm(self, token, key="start-key-1"):
        return server.tool_confirm_print_start({"printer": "a1", "token": token, "idempotency_key": key})

    def test_upload_reports_what_the_file_was_sliced_for(self):
        result = self.upload()
        self.assertEqual(result["model"], "Bambu Lab A1")
        self.assertEqual((result["nozzle"], result["filament"], result["time_s"]), ("0.4", "PLA", 1405))

    def test_weight_is_optional_because_orca_242_leaves_it_empty(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(make_3mf())) as src, zipfile.ZipFile(buf, "w") as dst:
            for name in src.namelist():
                dst.writestr(name, src.read(name).replace(b'key="weight" value="9.15"', b'key="weight" value=""'))
        file_id = server.upload_file("cubo.gcode.3mf", buf.getvalue())["file_id"]
        summary = payload(self.prepare(file_id))["summary"]
        self.assertIn("minuti", summary)
        self.assertNotIn("None", summary)

    def test_unknown_spool_is_never_silent(self):
        file_id = self.upload()["file_id"]
        self.assertIn("non noto", payload(self.prepare(file_id))["summary"])

    def test_weight_is_computed_from_length_when_slicer_leaves_it_empty(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/plate_1.gcode", "G28\n")
            z.writestr("Metadata/project_settings.config", json.dumps({
                "printer_model": "Bambu Lab A1", "nozzle_diameter": ["0.4"],
                "filament_density": ["1.24"], "filament_diameter": ["1.75"]}))
            z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="prediction" value="1003"/><metadata key="weight" value=""/><filament id="1" type="PLA" used_m="0.42" used_g="0.00"/></plate></config>')
        self.assertAlmostEqual(server.inspect_3mf(buf.getvalue())["weight_g"], 1.25, places=2)

    def test_upload_rejects_what_cannot_be_started(self):
        cases = [("cubo.stl", make_3mf()), ("cubo.gcode.3mf", b"not a zip"),
                 ("cubo.gcode.3mf", make_3mf(plates=2)),
                 ("../cubo.gcode.3mf", make_3mf()), ('a"b.gcode.3mf', make_3mf())]
        for name, data in cases:
            with self.subTest(name=name), self.assertRaises(server.ToolError):
                server.upload_file(name, data)
        self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_uploaded_files_stay_in_bff_until_start(self):
        file_id = self.upload()["file_id"]
        self.assertTrue((Path(self.tmp.name) / "files" / f"{file_id}.gcode.3mf").exists())
        self.upload_ftp.assert_not_called()

    def test_prepare_refuses_file_for_another_printer_model(self):
        file_id = self.upload(model="Bambu Lab A1 mini")["file_id"]
        with self.assertRaisesRegex(server.ToolError, "modello"):
            self.prepare(file_id)

    def test_prepare_refuses_wrong_nozzle_filament_or_ams(self):
        file_id = self.upload()["file_id"]
        for field, value in (("nozzles", [{"nozzle_diameter": "0.6"}]), ("vt_tray", [{"tray_type": "PETG"}]),
                             # An attached AMS no longer refuses by itself (the user 2026-11-12): read live,
                             # refused only when nothing loaded fits (tests/test_onboard.py Ams).
                             ("nozzles", [])):
            original = self.state[field]
            self.state[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(server.ToolError):
                self.prepare(file_id)
            self.state[field] = original

    def test_prepare_refuses_when_no_filament_is_loaded(self):
        file_id = self.upload()["file_id"]
        self.state["tray_now"] = 255
        with self.assertRaisesRegex(server.ToolError, "nessun filamento"):
            self.prepare(file_id)
        self.state["tray_now"] = 254
        self.prepare(file_id)

    def test_prepare_refuses_unknown_file_busy_printer_and_hms(self):
        with self.assertRaisesRegex(server.ToolError, "not on Pygmy"):
            self.prepare(999)
        file_id = self.upload()["file_id"]
        self.state["state"] = "RUNNING"
        with self.assertRaises(server.ToolError):
            self.prepare(file_id)
        self.state.update(state="IDLE", hms_errors=[{"code": 1}])
        with self.assertRaisesRegex(server.ToolError, "HMS.*1"):
            self.prepare(file_id)
        self.state.update(hms_errors=[{"code": 1, "description": "Z axis homing failed"}])
        with self.assertRaisesRegex(server.ToolError, "HMS.*Z axis homing failed"):
            self.prepare(file_id)

    def test_prepare_shows_a_fresh_photo_and_sends_nothing(self):
        file_id = self.upload()["file_id"]
        content = self.prepare(file_id)
        self.assertEqual(base64.b64decode(next(c["data"] for c in content if c["type"] == "image")), JPEG)
        data = payload(content)
        self.assertTrue(data["photo_url"].startswith("http://bff.test:8765/photo/"))
        self.assertIn("cubo.gcode.3mf", data["summary"])
        self.link.start_print.assert_not_called()
        self.upload_ftp.assert_not_called()
        self.assertEqual(server.PHOTOS[data["photo_url"].rsplit("/", 1)[1]][1], JPEG)

    def test_start_uploads_then_starts_once(self):
        data = make_3mf()
        file_id = server.upload_file("cubo.gcode.3mf", data)["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        self.assertEqual(payload(self.confirm(token))["outcome"], "confirmed")
        self.assertEqual(self.upload_ftp.call_args.args, ("h", "c", "cubo.gcode.3mf", data))
        self.upload_ftp.assert_called_once()
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf")
        with self.assertRaises(server.ToolError):
            self.confirm(token, "start-key-2")
        self.assertEqual(payload(self.confirm(token))["replayed"], True)
        self.link.start_print.assert_called_once()

    def test_confirm_without_valid_token_locks_nothing_and_sends_nothing(self):
        self.upload()
        for token in (None, "", "bogus"):
            with self.subTest(token=token):
                with self.assertRaises(server.ToolError):
                    self.confirm(token)
                self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)
                self.link.start_print.assert_not_called()
                self.upload_ftp.assert_not_called()

    def test_busy_printer_is_a_clear_refusal_without_retry(self):
        file_id = self.upload()["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        self.state["state"] = "RUNNING"
        with self.assertRaisesRegex(server.ToolError, "rejected"):
            self.confirm(token)
        self.upload_ftp.assert_not_called()
        self.link.start_print.assert_not_called()

    def test_upload_filaments_reads_every_filament(self):
        data = make_3mf(filaments=("PLA", "PLA"), colors=("#FFFFFF", "#FF0000"))
        file_id = server.upload_file("cubo.gcode.3mf", data)["file_id"]
        self.assertEqual(server.upload_filaments(file_id), [
            {"id": 1, "type": "PLA", "color": "#FFFFFF"},
            {"id": 2, "type": "PLA", "color": "#FF0000"}])

    def two_colour_start(self, second_color):
        self.state["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                                                {"id": 2, "tray_type": "PLA", "tray_color": "FF0000FF"}]}]
        data = make_3mf(filaments=("PLA", "PLA"), colors=("#FFFFFF", second_color))
        file_id = server.upload_file("cubo.gcode.3mf", data)["file_id"]
        return payload(self.prepare(file_id))["confirm_token"]

    def test_two_colour_start_maps_each_filament_to_its_own_ams_slot(self):
        token = self.two_colour_start("#FF0000")
        self.assertEqual(payload(self.confirm(token))["outcome"], "confirmed")
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf", (0, 2))

    def test_two_colour_start_refuses_when_a_colour_has_no_ams_slot(self):
        with self.assertRaisesRegex(server.ToolError, "Nessuno slot AMS per il filamento 2"):
            self.two_colour_start("#000000")
        self.link.start_print.assert_not_called()
        self.upload_ftp.assert_not_called()

    def picked(self, feed, filaments=("PLA", "PLA"), colors=("#FF0000", "#FFFFFF")):
        """A sliced file with the spools picked in Print now, as _slice_request stores them."""
        file_id = server.upload_file("cubo.gcode.3mf", make_3mf(filaments=filaments, colors=colors))["file_id"]
        server.DB.execute("UPDATE uploads SET feed=? WHERE file_id=?", (json.dumps(feed), file_id))
        return file_id

    def start(self, file_id):
        token = payload(self.prepare(file_id))["confirm_token"]
        self.assertEqual(payload(self.confirm(token))["outcome"], "confirmed")

    def test_picked_spools_drive_the_start(self):
        """the user 2026-11-15: red/white cube on Alpha, three PLA loaded, none with the file's exact colour."""
        self.state["ams"] = [{"id": 0, "tray": [{"id": i, "tray_type": "PLA", "tray_color": "C12E1FFF"} for i in range(3)]}]
        self.start(self.picked([0, 2]))
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf", (0, 2))

    def test_sixteen_colours_on_four_ams(self):
        self.state["ams"] = [{"id": u, "tray": [{"id": t, "tray_type": "PLA", "tray_color": "808080FF"} for t in range(4)]}
                             for u in range(4)]
        feed = list(range(15, -1, -1))
        self.start(self.picked(feed, filaments=("PLA",) * 16, colors=tuple(f"#{i:02X}0000" for i in range(16))))
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf", tuple(feed))

    def test_mixed_materials_on_picked_slots(self):
        self.state["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                                                {"id": 1, "tray_type": "PETG", "tray_color": "FFFFFFFF"}]}]
        self.start(self.picked([0, 1], filaments=("PLA", "PETG")))
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf", (0, 1))

    def test_picked_slot_emptied_refuses_before_anything_is_sent(self):
        self.state["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"}]}]
        with self.assertRaisesRegex(server.ToolError, "AMS slot A2 is empty now"):
            self.prepare(self.picked([0, 1]))
        self.link.start_print.assert_not_called()
        self.upload_ftp.assert_not_called()

    def test_picked_slot_with_other_material_refuses(self):
        self.state["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                                                {"id": 1, "tray_type": "PETG", "tray_color": "FFFFFFFF"}]}]
        with self.assertRaisesRegex(server.ToolError, "AMS slot A2 now holds PETG, the file needs PLA"):
            self.prepare(self.picked([0, 1]))

    def test_slot_changed_between_prepare_and_confirm_sends_nothing(self):
        self.state["ams"] = [{"id": 0, "tray": [{"id": i, "tray_type": "PLA", "tray_color": "FF0000FF"} for i in range(2)]}]
        token = payload(self.prepare(self.picked([0, 1])))["confirm_token"]
        self.state["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"}]}]
        with self.assertRaises(server.ToolError):
            self.confirm(token)
        self.link.start_print.assert_not_called()
        self.upload_ftp.assert_not_called()

    def test_no_ams_multi_colour_prints_on_the_one_spool(self):
        self.state.pop("ams", None)
        self.start(self.picked([254, 254]))
        self.link.start_print.assert_called_once_with("cubo.gcode.3mf")

    def test_no_ams_mixed_materials_refused(self):
        """One spool cannot print PLA and PETG."""
        self.state.pop("ams", None)
        with self.assertRaisesRegex(server.ToolError, "PETG/PLA"):
            self.prepare(self.picked([254, 254], filaments=("PLA", "PETG")))

    def test_interrupted_upload_sends_no_start(self):
        self.upload_ftp.side_effect = server.ftps.UploadError("x")
        file_id = self.upload()["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        with self.assertRaisesRegex(server.ToolError, "rejected"):
            self.confirm(token)
        self.link.start_print.assert_not_called()
        self.prepare(file_id)  # The printer was never locked.

    def locked_after_silent_start(self):
        self.link.start_print.side_effect = None
        self.link.start_print.return_value = True  # sent, but the printer never starts
        file_id = self.upload()["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        with self.assertRaisesRegex(server.ToolError, "unknown"):
            self.confirm(token)
        idle = self.state["state"]
        self.state["state"] = "PREPARE"  # preparing: the start may still be taking effect, so the lock holds
        with self.assertRaisesRegex(server.ToolError, "recheck"):
            self.prepare(file_id)
        self.state["state"] = idle

    def test_an_idle_printer_after_a_silent_start_is_freed_without_recheck(self):
        self.link.start_print.side_effect = None
        self.link.start_print.return_value = True  # sent, but the printer never starts and stays idle
        file_id = self.upload()["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        with self.assertRaisesRegex(server.ToolError, "unknown"):
            self.confirm(token)
        self.prepare(file_id)  # no recheck needed: the printer is doing nothing
        self.assertIsNone(server.DB.execute("SELECT 1 FROM locks WHERE printer='a1'").fetchone())

    def test_recheck_frees_a_start_that_never_began(self):
        self.locked_after_silent_start()
        result = payload(server.tool_recheck({"printer": "a1"}))
        self.assertEqual((result["outcome"], result["locked"]), ("not_applied", False))

    def test_recheck_confirms_a_start_that_began_late(self):
        self.locked_after_silent_start()
        self.state["state"] = "RUNNING"
        self.assertEqual(payload(server.tool_recheck({"printer": "a1"}))["outcome"], "confirmed")

    def test_start_not_sent_keeps_the_lock_until_recheck(self):
        self.link.start_print.side_effect = None
        self.link.start_print.return_value = False
        file_id = self.upload()["file_id"]
        token = payload(self.prepare(file_id))["confirm_token"]
        with self.assertRaisesRegex(server.ToolError, "unknown"):
            self.confirm(token)
        self.link.start_print.assert_called_once()
        self.assertEqual(payload(server.tool_recheck({"printer": "a1"}))["outcome"], "not_applied")


class UploadHttpTest(PrintStartTest):
    def setUp(self):
        super().setUp()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def put(self, data, auth=AUTH, name="cubo.gcode.3mf"):
        req = urllib.request.Request(f"{self.base}/upload/{name}", data=data, method="PUT")
        if auth:
            req.add_header("Authorization", "Bearer " + auth)
        return urllib.request.urlopen(req, timeout=5)

    def status_of(self, call):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            call()
        return ctx.exception.code

    def test_put_needs_token_and_control(self):
        self.assertEqual(self.status_of(lambda: self.put(make_3mf(), auth=None)), 401)
        server.CFG["control"] = False
        self.assertEqual(self.status_of(lambda: self.put(make_3mf())), 404)

    def test_put_uploads_and_bad_file_is_400_without_leak(self):
        self.assertEqual(json.load(self.put(make_3mf()))["model"], "Bambu Lab A1")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.put(b"junk")
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("errore", json.load(ctx.exception))

    def test_photo_link_works_without_token_only_while_valid(self):
        file_id = self.upload()["file_id"]
        url = payload(self.prepare(file_id))["photo_url"]
        path = url.removeprefix("http://bff.test:8765")
        resp = urllib.request.urlopen(self.base + path, timeout=5)
        self.assertEqual((resp.read(), resp.headers["Content-Type"]), (JPEG, "image/jpeg"))
        server.PHOTOS[path.rsplit("/", 1)[1]] = (0, JPEG)
        self.assertEqual(self.status_of(lambda: urllib.request.urlopen(self.base + path, timeout=5)), 404)
        self.assertEqual(self.status_of(lambda: urllib.request.urlopen(self.base + "/photo/nope", timeout=5)), 404)


# The base class would otherwise run twice through the subclass; keep HTTP-only tests there.
for _name in [n for n in dir(PrintStartTest) if n.startswith("test_")]:
    setattr(UploadHttpTest, _name, None)
