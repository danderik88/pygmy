"""Print-now server slicing: worker, slicer client, and the slicerd HTTP contract."""
import base64
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from pygmy import server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402

slicer = server.slicer  # the module server.py imports, so setup() reaches the worker

PROFILE = {"model": "A1", "command_timeout_s": 0.3, "supports": {"stop": True, "start": True}}
SECRET = "test-secret"


def make_3mf():
    """A minimal valid single-plate 3mf that inspect_3mf accepts."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\nM73 P30\n")
        z.writestr("Metadata/slice_info.config",
                   "G28\n<filament type=\"PLA\" id=\"0\" color=\"#FF0000\" used_m=\"1.0\" "
                   "start_gcode=\"\" end_gcode=\"\" />\n<key=\"prediction\" value=\"525\"/>\n"
                   "<key=\"weight\" value=\"3.2\"/>\n")
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"printer_model": "A1", "nozzle_diameter": ["0.4"],
                               "curr_bed_type": "Textured PEI Plate"}))
    return buf.getvalue()


def model_3mf(plates=1, colors=None):
    """A project 3mf with `plates` <plate> entries and the given filament_colour list."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/model_settings.config",
                   "<config>" + "".join(f'<plate><metadata key="plater_id" value="{i + 1}"/></plate>'
                                        for i in range(plates)) + "</config>")
        if colors is not None:
            z.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": colors}))
    return buf.getvalue()


class SlicerState:
    def __init__(self, slice_status=200, plates=1, health_status=200):
        self.slice_status = slice_status
        self.plates = plates
        self.health_status = health_status
        self.requests = []
        self.error_body = None  # reply body when slice_status is not 200
        self.hook = None  # called with the /slice body before the reply
        self.concurrent = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()


class SlicerHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(self.server.state.health_status, {"orca": "2.5.0", "profiles": 1})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/slice":
            return self._send(404, {"error": "not found"})
        st = self.server.state
        with st.lock:
            st.concurrent += 1
            st.max_concurrent = max(st.max_concurrent, st.concurrent)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length))
            st.requests.append(body)
            if st.hook:
                st.hook(body)
            if st.slice_status != 200:
                return self._send(st.slice_status, st.error_body or {"error": "fail"})
            plates = [{"gcode_3mf": base64.b64encode(make_3mf()).decode(), "seconds": 525, "grams": 3.2}
                      for _ in range(st.plates)]
            return self._send(200, {"plates": plates})
        finally:
            with st.lock:
                st.concurrent -= 1


class SlicerServer(ThreadingHTTPServer):
    def __init__(self, addr, state):
        super().__init__(addr, SlicerHandler)
        self.state = state

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class SlicerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp),
                          public_url="http://bff", orca_wake_url=None)
        server.open_db(str(tmp / "state.db"))
        server.library.setup(str(tmp / "files"))
        self.link = server.LINKS["a1"] = direct_tests.FakeLink(state="IDLE")
        self.state = SlicerState()
        self.srv = SlicerServer(("127.0.0.1", 0), self.state)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        slicer.setup(self.srv.url, SECRET)

    def tearDown(self):
        slicer.setup("", "")
        server.SLICE_WAKE.clear()
        server.DB.close()
        server.LINKS.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def row(self, rid, *cols):
        return server.DB.execute("SELECT " + ", ".join(cols) + " FROM print_requests WHERE id=?", (rid,)).fetchone()

    def test_upload_goes_ready_with_file_ids(self):
        self.state.plates = 2
        rid = server.upload_model("a1", "cube.stl", b"solid x", engine="server",
                                  choices={"infill": 20}, slot="external")["request_id"]
        self.assertTrue(server.slice_once())
        self.assertFalse(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "ready")
        fids = json.loads(self.row(rid, "plates")[0])
        self.assertEqual(len(fids), 2)
        names = [server.DB.execute("SELECT name FROM uploads WHERE file_id=?", (f,)).fetchone()[0] for f in fids]
        self.assertEqual(names, ["cube_1.gcode.3mf", "cube_2.gcode.3mf"])
        self.assertEqual(self.state.requests[0]["filament_id"], "GFA01")

    def test_slice_error_marks_failed(self):
        self.state.slice_status = 422
        rid = server.upload_model("a1", "bad.stl", b"x", engine="server", slot="external")["request_id"]
        self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"), ("failed", "Could not slice this model: fail"))
        self.state.slice_status = 200
        rid2 = server.upload_model("a1", "good.stl", b"x", engine="server", slot="external")["request_id"]
        self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid2, "status")[0], "ready")

    def test_copies_from_request_column(self):
        rid = server.upload_model("a1", "cube.stl", b"solid x", engine="server",
                                  choices={"infill": 20}, slot="external")["request_id"]
        with server.DB:
            server.DB.execute("UPDATE print_requests SET copies=3 WHERE id=?", (rid,))
        self.assertTrue(server.slice_once())
        self.assertEqual(self.state.requests[0]["copies"], 3)
        self.assertEqual(self.row(rid, "status")[0], "ready")

    def test_cancel_while_working_stays_cancelled(self):
        self.state.slice_status = 422
        rid = server.upload_model("a1", "cancel.stl", b"x", engine="server", slot="external")["request_id"]

        def cancel(_body):
            with server.DB:
                server.DB.execute("UPDATE print_requests SET status='cancelled' WHERE id=?", (rid,))

        self.state.hook = cancel
        self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "cancelled")

    def test_slicer_down(self):
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        slicer.setup(f"http://127.0.0.1:{port}", SECRET)
        rid = server.upload_model("a1", "down.stl", b"x", engine="server", slot="external")["request_id"]
        start = time.monotonic()
        self.assertTrue(server.slice_once())
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.row(rid, "status", "note"), ("failed", "Slicer not reachable"))

    def test_one_slice_at_a_time(self):
        server.upload_model("a1", "one.stl", b"x", engine="server", slot="external")
        server.upload_model("a1", "two.stl", b"x", engine="server", slot="external")
        orig_wait = server.SLICE_WAKE.wait
        server.SLICE_WAKE.wait = lambda *a, **k: (_ for _ in ()).throw(SystemExit)
        try:
            thread = threading.Thread(target=server.slice_worker, daemon=True)
            thread.start()
            thread.join(timeout=10)
        finally:
            server.SLICE_WAKE.wait = orig_wait
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.state.max_concurrent, 1)
        rows = server.DB.execute("SELECT status FROM print_requests ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["ready", "ready"])

    def test_orca_engine_still_wakes_orca(self):
        called = []
        real_wake = server.wake_orca
        server.wake_orca = lambda rid: called.append(rid) or None
        self.addCleanup(setattr, server, "wake_orca", real_wake)
        o = server.upload_model("a1", "o.stl", b"x", engine="orca")
        self.assertEqual(o["outcome"], "queued for Orca-Cad")
        self.assertEqual(called, [o["request_id"]])
        server.SLICE_WAKE.clear()
        s = server.upload_model("a1", "s.stl", b"x", engine="server", slot="external")
        self.assertEqual(s["outcome"], "queued for slicing")
        self.assertEqual(called, [o["request_id"]])
        self.assertTrue(server.SLICE_WAKE.is_set())

    def test_default_engine_orca_without_slot(self):
        """Slicer available but no slot: keep the default on Orca-Cad."""
        called = []
        real_wake = server.wake_orca
        server.wake_orca = lambda rid: called.append(rid) or None
        self.addCleanup(setattr, server, "wake_orca", real_wake)
        self.assertTrue(slicer.available())
        reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-cube"})
        out = json.loads(reply[0]["text"])
        self.assertEqual(out["engine"], "orca")
        self.assertEqual(called, [out["request_id"]])

    def test_plain_put_still_orca(self):
        called = []
        real_wake = server.wake_orca
        server.wake_orca = lambda rid: called.append(rid) or None
        self.addCleanup(setattr, server, "wake_orca", real_wake)
        self.assertTrue(slicer.available())
        out = server.upload_model("a1", "plain.stl", b"solid x")
        self.assertEqual(out["engine"], "orca")
        self.assertEqual(out["outcome"], "queued for Orca-Cad")
        self.assertEqual(called, [out["request_id"]])

    def test_copies_limits_by_engine(self):
        url = "https://www.printables.com/model/1-cube"
        with self.assertRaisesRegex(server.ToolError, "copies: da 1 a 20"):
            server.tool_request_print({"printer": "a1", "url": url, "engine": "server",
                                       "slot": "external", "copies": 21})
        out = json.loads(server.tool_request_print({"printer": "a1", "url": url, "engine": "orca",
                                                    "copies": 20})[0]["text"])
        self.assertEqual(out["copies"], 20)
        with self.assertRaisesRegex(server.ToolError, "copies: da 1 a 20"):  # slicer limit for every engine
            server.tool_request_print({"printer": "a1", "url": url, "engine": "orca", "copies": 21})

    # Draft requests (Task 3): fetch, stop at draft with facts, slice only on slice_request.

    def test_upload_draft_returns_facts(self):
        out = server.upload_model("a1", "two.3mf", model_3mf(colors=["#FF0000", "#00ff00"]), draft=True)
        self.assertEqual(out["facts"], {"plates": 1, "colors": ["#FF0000", "#00FF00"]})
        self.assertEqual(self.row(out["id"], "status")[0], "draft")
        self.assertFalse(server.slice_once())
        self.assertEqual(self.state.requests, [])
        listed = json.loads(server.tool_print_requests({"printer": "a1"})[0]["text"])["requests"]
        self.assertEqual(listed[0]["facts"], out["facts"])

    def test_link_fetch_stops_at_draft(self):
        with mock.patch.object(server.fetch, "fetch_model", return_value=("cube.stl", b"solid x", [])):
            reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-cube",
                                               "draft": True})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
            self.assertFalse(server.slice_once())
        self.assertEqual(self.row(rid, "status", "file_name")[0:2], ("draft", "cube.stl"))
        self.assertEqual(json.loads(self.row(rid, "facts")[0]), {"plates": 1, "colors": []})
        self.assertEqual(self.state.requests, [])

    def test_multi_plate_waits_for_a_plate_pick(self):
        # the user 2026-11-15: no refusal any more; the app's picker says which plate, the slice gets only that one
        data = model_3mf(plates=3)
        with mock.patch.object(server.fetch, "fetch_model", return_value=("big.3mf", data, [])):
            reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-cube",
                                               "draft": True})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "draft")
        args = {"printer": "a1", "id": rid, "slots": ["external"], "choices": {}, "copies": 1, "standard": True}
        with self.assertRaisesRegex(server.ToolError, "Pick a plate to print"):
            server.tool_slice_request(args)
        with self.assertRaisesRegex(server.ToolError, "Pick a plate to print"):
            server.tool_slice_request({**args, "plate_no": 9})
        server.tool_slice_request({**args, "plate_no": 2})
        self.assertEqual(json.loads(self.row(rid, "choices")[0]).get("plate_no"), 2)

    def test_slice_request_only_from_draft(self):
        rid = server.upload_model("a1", "cube.stl", b"solid x", engine="server", slot="external")["request_id"]
        args = {"printer": "a1", "id": rid, "slots": ["external"], "choices": {}, "copies": 1, "standard": True}
        with self.assertRaisesRegex(server.ToolError, "This job is not waiting for slicing"):
            server.tool_slice_request(args)
        did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
        server.tool_slice_request({**args, "id": did, "copies": 2})
        self.assertEqual(self.row(did, "status", "stage", "copies"), ("pending", "slice", 2))
        with self.assertRaisesRegex(server.ToolError, "This job is not waiting for slicing"):
            server.tool_slice_request({**args, "id": did})
        while server.slice_once():
            pass
        self.assertEqual(self.row(did, "status")[0], "ready")
        self.assertEqual(self.state.requests[-1]["copies"], 2)
        self.assertEqual(self.state.requests[-1]["filament_id"], "GFA01")
        with self.assertRaises(server.ToolError):
            server.tool_slice_request({**args, "id": did, "copies": 21})

    def test_ready_job_can_be_sliced_again(self):
        """A setting changed after the slice re-slices the ready job; a started one is still refused."""
        did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
        args = {"printer": "a1", "id": did, "slots": ["external"], "choices": {}, "copies": 1, "standard": True}
        server.tool_slice_request(args)
        while server.slice_once():
            pass
        self.assertEqual(self.row(did, "status")[0], "ready")
        self.assertIsNotNone(self.row(did, "file_id")[0])
        out = json.loads(server.tool_slice_request({**args, "copies": 2})[0]["text"])
        self.assertEqual(out["outcome"], "queued for slicing")
        self.assertEqual(self.row(did, "status")[0], "pending")
        self.assertIsNone(self.row(did, "file_id")[0])
        with server.DB:
            server.DB.execute("UPDATE print_requests SET status='started' WHERE id=?", (did,))
        with self.assertRaisesRegex(server.ToolError, "This job is not waiting for slicing"):
            server.tool_slice_request(args)

    def test_slice_request_wrong_slot_count(self):
        did = server.upload_model("a1", "two.3mf", model_3mf(colors=["#FF0000", "#00FF00"]), draft=True)["id"]
        args = {"printer": "a1", "id": did, "choices": {}, "copies": 1, "standard": True}
        for bad in (["external"], ["external"] * 3, [], "external"):
            with self.assertRaisesRegex(server.ToolError, "Pick one spool per colour"):
                server.tool_slice_request({**args, "slots": bad})
        server.tool_slice_request({**args, "slots": ["external", "external"]})  # same spool twice is fine
        self.assertEqual(json.loads(self.row(did, "slots")[0]), ["external", "external"])

    def test_draft_cancel(self):
        did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
        server.tool_cancel_request({"printer": "a1", "request_id": did})
        self.assertEqual(self.row(did, "status")[0], "cancelled")

    def test_blocked_code_then_draft(self):
        with mock.patch.object(server.makerworld, "fetch",
                               side_effect=server.makerworld.LoginNeeded("MakerWorld login needed")):
            reply = server.tool_request_print({"printer": "a1", "url": "https://makerworld.com/en/models/123-cube",
                                               "draft": True})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "blocked")
        with mock.patch.object(server.makerworld, "login_code"):
            server.tool_mw_login_code({"code": "123456"})
        with mock.patch.object(server.makerworld, "fetch", return_value=("cube.3mf", model_3mf(), [])):
            self.assertTrue(server.slice_once())
            self.assertFalse(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "draft")
        self.assertEqual(self.state.requests, [])

    def test_bundle_draft_picks_the_files(self):
        name, data = server.fetch.bundle([("a.stl", b"solid a"), ("b.stl", b"solid b")])
        with mock.patch.object(server.fetch, "fetch_model", return_value=(name, data, [])):
            reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-x",
                                               "draft": True})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(json.loads(self.row(rid, "facts")[0])["files"], ["a.stl", "b.stl"])
        args = {"printer": "a1", "id": rid, "slots": ["external"], "choices": {}, "copies": 1, "standard": True}
        for bad in ([], [2], [0, 0], ["0"], True):
            with self.assertRaisesRegex(server.ToolError, "Pick at least one file"):
                server.tool_slice_request({**args, "choices": {"files": bad}})
        server.tool_slice_request({**args, "choices": {"files": [1], "infill": 30}})
        self.assertEqual(json.loads(self.row(rid, "choices")[0]), {"files": [1]})  # Standard keeps files
        while server.slice_once():
            pass
        self.assertEqual(self.row(rid, "status")[0], "ready")
        self.assertEqual(self.state.requests[-1]["filename"], "b.stl")

    def test_bundle_missing_files_slices_them_all(self):
        name, data = server.fetch.bundle([("a.stl", b"solid a"), ("b.stl", b"solid b")])
        with mock.patch.object(server.fetch, "fetch_model", return_value=(name, data, [])):
            rid = json.loads(server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-x",
                                                        "draft": True})[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        server.tool_slice_request({"printer": "a1", "id": rid, "slots": ["external"], "choices": {}, "copies": 1})
        self.assertEqual(json.loads(self.row(rid, "choices")[0]), {"files": [0, 1]})

    def test_standard_keeps_reorient(self):
        did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
        server.tool_slice_request({"printer": "a1", "id": did, "slots": ["external"],
                                   "choices": {"infill": 15, "reorient": True}, "copies": 1, "standard": True})
        self.assertEqual(self.row(did, "choices")[0], json.dumps({"reorient": True}))

    def test_klipper_slices_with_the_spool_set_in_the_app(self):
        """No AMS on Klipper: the slice uses the material (and profile) set with set_spool, not PLA."""
        (Path(server.CFG["profiles"]) / "sv08.json").write_text(json.dumps({"model": "Sovol SV08"}))
        link = server.MoonrakerLink("sv08", "127.0.0.1", port=1)
        link.snapshot = lambda: {"connected": True, "state": "IDLE", "ams": [], "vt_tray": []}
        server.LINKS["sv08"] = link
        self.addCleanup(server.LINKS.pop, "sv08", None)
        with mock.patch.dict(server.CFG["printers"], {"sv08": 2}):
            server.tool_set_spool({"printer": "sv08", "profile_id": "GFG99", "color": "FF0000FF"})
            did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
            server.tool_slice_request({"printer": "sv08", "id": did, "slots": ["external"], "choices": {}, "copies": 1})
            self.assertTrue(server.slice_once())
        self.assertEqual(self.state.requests[-1]["filaments"], [{"filament_id": "GFG99", "filament_type": "PETG", "filament_name": "Generic PETG"}])

    def test_standard_sends_printer_plate(self):
        """Orca defaults to Cool Plate and refuses PLA: the printer's plate always goes along."""
        server.DB.execute("INSERT OR REPLACE INTO settings VALUES ('a1', 'plate_type', 'Textured PEI Plate')")
        did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
        server.tool_slice_request({"printer": "a1", "id": did, "slots": ["external"],
                                   "choices": {"plate": "Cool Plate"}, "copies": 1, "standard": True})
        self.assertTrue(server.slice_once())
        self.assertEqual(self.state.requests[-1]["choices"].get("plate"), "Textured PEI Plate")

    def test_slice_request_other_printer(self):
        (Path(server.CFG["profiles"]) / "p1s.json").write_text(json.dumps({"model": "P1S"}))
        server.LINKS["p1s"] = direct_tests.FakeLink(state="IDLE")
        with mock.patch.dict(server.CFG, {"printers": {"a1": 1, "p1s": 2}}):
            did = server.upload_model("a1", "cube.stl", b"solid x", draft=True)["id"]
            server.tool_slice_request({"printer": "p1s", "id": did, "slots": ["external"], "choices": {"infill": 15},
                                       "copies": 1, "standard": False})
            self.assertEqual(self.row(did, "printer", "choices"), ("p1s", json.dumps({"infill": 15})))
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(did, "status")[0], "ready")
        self.assertEqual(self.state.requests[0]["machine"], "Bambu Lab P1S 0.4 nozzle")

    def test_picked_slots_are_stored_for_the_start(self):
        """the user's real two-colour cube, two AMS slots picked: the sliced file remembers them."""
        raw = dict(self.link.snapshot())
        raw["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "GFA01"},
                                         {"id": 2, "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFA01"}]}]
        model = (Path(__file__).parent / "fixtures" / "cubo-bicolore.3mf").read_bytes()
        with mock.patch.object(server, "read_status", return_value=raw):
            did = server.upload_model("a1", "cubo-bicolore.3mf", model, draft=True)["id"]
            server.tool_slice_request({"printer": "a1", "id": did, "slots": ["A1", "A3"], "choices": {}, "copies": 1})
            self.assertTrue(server.slice_once())
        self.assertEqual(len(self.state.requests[-1]["filaments"]), 2)
        fid = server.DB.execute("SELECT MAX(file_id) FROM uploads").fetchone()[0]
        feed = server.DB.execute("SELECT feed FROM uploads WHERE file_id=?", (fid,)).fetchone()[0]
        self.assertEqual(json.loads(feed), [0, 2])

    def test_no_ams_repeats_spool(self):
        """Three model colours, no AMS: the one loaded spool is sent three times."""
        raw = dict(self.link.snapshot())
        raw.pop("ams", None)
        with mock.patch.object(server, "read_status", return_value=raw):
            did = server.upload_model("a1", "three.3mf",
                                      model_3mf(colors=["#FF0000", "#00FF00", "#0000FF"]), draft=True)["id"]
            server.tool_slice_request({"printer": "a1", "id": did, "slots": ["external"] * 3,
                                       "choices": {}, "copies": 1})
            self.assertTrue(server.slice_once())
        body = self.state.requests[-1]
        self.assertEqual([{"filament_id": "GFA01", "filament_type": "PLA", "filament_name": "Bambu PLA Matte"}] * 3, body["filaments"])
        self.assertNotIn("filament_id", body)  # legacy fields only for a single colour

    def test_reset_stuck_slices(self):
        with server.DB:
            server.DB.execute("INSERT INTO print_requests (printer, url, created, status, source, engine) "
                              "VALUES ('a1', '', 0, 'working', 'upload', 'server')")
            server.DB.execute("INSERT INTO print_requests (printer, url, created, status, source, engine) "
                              "VALUES ('a1', '', 0, 'working', 'upload', 'orca')")
        server.reset_stuck_slices()
        rows = server.DB.execute("SELECT status, engine FROM print_requests ORDER BY id").fetchall()
        self.assertEqual(rows, [("pending", "server"), ("working", "orca")])

    def test_slice_worker_resets_stuck_first(self):
        calls = []
        real_reset, orig_wait = server.reset_stuck_slices, server.SLICE_WAKE.wait
        server.reset_stuck_slices = lambda: calls.append(1)
        server.SLICE_WAKE.wait = lambda *a, **k: (_ for _ in ()).throw(SystemExit)
        try:
            with self.assertRaises(SystemExit):
                server.slice_worker()
        finally:
            server.reset_stuck_slices, server.SLICE_WAKE.wait = real_reset, orig_wait
        self.assertEqual(calls, [1])

    def test_cancel_during_slice_removes_uploaded_files(self):
        rid = server.upload_model("a1", "cancelme.stl", b"solid x", engine="server", slot="external")["request_id"]

        def cancel(_body):
            with server.DB:
                server.DB.execute("UPDATE print_requests SET status='cancelled' WHERE id=?", (rid,))

        self.state.hook = cancel
        self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "cancelled")
        files = json.loads(server.tool_ready_files({"printer": "a1"})[0]["text"])["files"]
        self.assertEqual(files, [])

    def test_slot_without_filament_refused(self):
        self.link.feed({"print": {"vt_tray": {"tray_info_idx": None}}})
        with self.assertRaisesRegex(server.ToolError, "Set this spool in Filament first"):
            server.upload_model("a1", "x.stl", b"x", engine="server", slot="external")

    def test_printables_request_goes_ready(self):
        with mock.patch.object(server.fetch, "fetch_model",
                               return_value=("cube.stl", b"solid x", ["other.3mf"])):
            reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-cube",
                                               "engine": "server", "slot": "external"})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status")[0], "ready")
        fids = json.loads(self.row(rid, "plates")[0])
        self.assertEqual(len(fids), 1)
        self.assertEqual(self.row(rid, "note")[0], "Also on this page: other.3mf")

    def test_makerworld_blocked_then_requeued(self):
        with mock.patch.object(server.makerworld, "fetch",
                               side_effect=server.makerworld.LoginNeeded("MakerWorld login needed")):
            with mock.patch.object(server.makerworld, "login_start"):
                reply = server.tool_request_print({"printer": "a1", "url": "https://makerworld.com/en/models/123-cube",
                                                   "engine": "server", "slot": "external"})
                rid = json.loads(reply[0]["text"])["request_id"]
                self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"), ("blocked", "MakerWorld login needed"))
        with mock.patch.object(server.makerworld, "login_code"):
            reply2 = server.tool_mw_login_code({"code": "123456"})
            self.assertEqual(json.loads(reply2[0]["text"])["requeued"], 1)
        self.assertEqual(self.row(rid, "status")[0], "pending")

    def test_fetch_error_note_on_failed(self):
        with mock.patch.object(server.fetch, "fetch_model",
                               side_effect=server.fetch.FetchError("File is over 100 MB")):
            reply = server.tool_request_print({"printer": "a1", "url": "https://www.printables.com/model/1-cube",
                                               "engine": "server", "slot": "external"})
            rid = json.loads(reply[0]["text"])["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"), ("failed", "File is over 100 MB"))

    def test_too_big_note_lists_fitting_printers(self):
        profile = Path(server.CFG["profiles"]) / "p1s.json"
        profile.write_text(json.dumps({"model": "P1S"}))
        self.state.slice_status = 422
        self.state.error_body = {"error": "too_big", "size": [200, 150, 90], "bed": [180, 180, 180],
                                 "fits_on": ["Bambu Lab P1S 0.4 nozzle"]}
        with mock.patch.dict(server.CFG, {"printers": {"a1": 1, "p1s": 2}}), \
                mock.patch.object(server, "app_names_models", lambda: ({"p1s": "Workshop P1S"}, {})):
            rid = server.upload_model("a1", "big.stl", b"x", engine="server", slot="external")["request_id"]
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"),
                         ("failed", "Too big for this printer: 200 × 150 × 90 mm, plate is 180 × 180 × 180 mm. "
                                    "Fits on: Workshop P1S"))
        self.assertEqual(self.state.requests[0]["other_machines"], ["Bambu Lab P1S 0.4 nozzle"])

    def test_unreadable_file_says_so(self):
        self.state.slice_status = 422
        self.state.error_body = {"error": "orca exit 250 (code -6): The input model file to the slicer can not be parsed."}
        rid = server.upload_model("a1", "odd.step", b"x", engine="server", slot="external")["request_id"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"),
                         ("failed", "The slicer can't read this file: export it as STL or 3MF and try again"))

    def test_any_orca_failure_shows_orcas_words(self):
        self.state.slice_status = 422
        self.state.error_body = {"error": "orca exit 250 (code -6): Object is empty after slicing"}
        rid = server.upload_model("a1", "x.stl", b"x", engine="server", slot="external")["request_id"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(server.slice_once())
        self.assertEqual(self.row(rid, "status", "note"), ("failed", "Could not slice this model: Object is empty after slicing"))

    def test_secret_not_in_note(self):
        self.state.slice_status = 422
        rid = server.upload_model("a1", "bad.stl", b"x", engine="server", slot="external")["request_id"]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertTrue(server.slice_once())
        self.assertNotIn(SECRET, self.row(rid, "note")[0])
        self.assertNotIn(SECRET, buf.getvalue())


class SlicerUnit(unittest.TestCase):
    def start(self, state):
        srv = SlicerServer(("127.0.0.1", 0), state)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def tearDown(self):
        slicer.setup("", "")

    def test_machine_preset(self):
        self.assertEqual(slicer.machine_preset("A1"), "Bambu Lab A1 0.4 nozzle")
        self.assertEqual(slicer.machine_preset("P1S", 0.6), "Bambu Lab P1S 0.6 nozzle")
        self.assertEqual(slicer.machine_preset("A1", None), "Bambu Lab A1 0.4 nozzle")

    def test_configured_and_available_off(self):
        slicer.setup("", "")
        self.assertFalse(slicer.configured())
        self.assertFalse(slicer.available())

    def test_available_health_ok_and_cached(self):
        srv = self.start(SlicerState())
        slicer.setup(srv.url, SECRET)
        self.assertTrue(slicer.available())
        self.assertTrue(slicer.available())

    def test_available_health_bad(self):
        srv = self.start(SlicerState(health_status=503))
        slicer.setup(srv.url, SECRET)
        self.assertFalse(slicer.available())

    def test_slice_422(self):
        srv = self.start(SlicerState(slice_status=422))
        slicer.setup(srv.url, SECRET)
        with self.assertRaisesRegex(slicer.SliceError, "Could not slice this model"):
            slicer.slice(b"x", "a.stl", "M", [{"filament_id": "GFA01", "filament_type": "PLA"}], {}, False, 1)

    def test_slice_5xx_or_down(self):
        srv = self.start(SlicerState(slice_status=500))
        slicer.setup(srv.url, SECRET)
        with self.assertRaisesRegex(slicer.SliceError, "Slicer not reachable"):
            slicer.slice(b"x", "a.stl", "M", [{"filament_id": "GFA01", "filament_type": "PLA"}], {}, False, 1)
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        slicer.setup(f"http://127.0.0.1:{port}", SECRET)
        with self.assertRaisesRegex(slicer.SliceError, "Slicer not reachable"):
            slicer.slice(b"x", "a.stl", "M", [{"filament_id": "GFA01", "filament_type": "PLA"}], {}, False, 1)

    def test_secret_never_in_exception(self):
        srv = self.start(SlicerState(slice_status=422))
        slicer.setup(srv.url, SECRET)
        try:
            slicer.slice(b"x", "a.stl", "M", [{"filament_id": "GFA01", "filament_type": "PLA"}], {}, False, 1)
        except slicer.SliceError as err:
            self.assertNotIn(SECRET, str(err))

    def test_secret_not_sent_on_redirect(self):
        seen = {"first": None, "second": None}

        class First(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["first"] = self.headers.get("Authorization")
                self.send_response(302)
                self.send_header("Location", self.server.target)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class Second(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen["second"] = self.headers.get("Authorization")
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

        second = ThreadingHTTPServer(("127.0.0.1", 0), Second)
        first = ThreadingHTTPServer(("127.0.0.1", 0), First)
        first.target = f"http://127.0.0.1:{second.server_address[1]}/slice"
        for srv in (first, second):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        slicer.setup(f"http://127.0.0.1:{first.server_address[1]}", SECRET)
        with self.assertRaises(slicer.SliceError):
            slicer.slice(b"x", "a.stl", "M", [{"filament_id": "GFA01", "filament_type": "PLA"}], {}, False, 1)
        self.assertEqual(seen["first"], "Bearer " + SECRET)
        self.assertIsNone(seen["second"])


if __name__ == "__main__":
    unittest.main()
