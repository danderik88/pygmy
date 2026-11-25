"""Filament and New print tabs: nozzle, spool, plate, ready files, MakerWorld requests."""
import io
import json
import secrets
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from pygmy import server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402
from test_bff_tools_more import PROFILE  # noqa: E402


def body(reply):
    return json.loads(reply[-1]["text"])


def make_3mf():
    """A minimal valid single-plate 3mf that upload_file accepts."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\n")
        z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab A1", "nozzle_diameter": ["0.4"]}))
        z.writestr("Metadata/slice_info.config",
                   '<config><plate><metadata key="prediction" value="1405"/><metadata key="weight" value="9.15"/>'
                   '<filament id="1" type="PLA" used_g="9"/></plate></config>')
    return buf.getvalue()


class FilamentNewPrint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp), public_url="http://bff")
        server.open_db(str(tmp / "state.db"))
        server.library.setup(str(tmp / "files"))
        self.link = server.LINKS["a1"] = direct_tests.FakeLink(state="IDLE")

    def key(self):
        return "key-" + secrets.token_hex(6)

    def test_set_nozzle_payload(self):
        server.tool_set_nozzle({"printer": "a1", "diameter": 0.6, "type": "hardened_steel", "idempotency_key": self.key()})
        sent = [p for p in self.link.published if "system" in p][-1]["system"]
        self.assertEqual((sent["command"], sent["accessory_type"], sent["nozzle_diameter"], sent["nozzle_type"]),
                         ("set_accessories", "nozzle", 0.6, "hardened_steel"))

    def test_set_nozzle_rejects_bad_values(self):
        for bad in ({"diameter": 0.5, "type": "stainless_steel"}, {"diameter": True, "type": "stainless_steel"},
                    {"diameter": 0.4, "type": "brass"}, {"diameter": 0.4, "type": ["x"]}):
            with self.assertRaises(server.ToolError):
                server.tool_set_nozzle({"printer": "a1", "idempotency_key": self.key(), **bad})
        self.assertEqual(self.link.published, [])

    def test_set_nozzle_refused_while_printing(self):
        self.link.feed({"print": {"gcode_state": "RUNNING"}})
        with self.assertRaises(server.ToolError):
            server.tool_set_nozzle({"printer": "a1", "diameter": 0.4, "type": "stainless_steel", "idempotency_key": self.key()})

    def test_set_spool_payload(self):
        server.tool_set_spool({"printer": "a1", "profile_id": "GFG99", "color": "FF0000FF", "idempotency_key": self.key()})
        sent = [p for p in self.link.published if p.get("print", {}).get("command") == "ams_filament_setting"][-1]["print"]
        self.assertEqual((sent["ams_id"], sent["tray_id"], sent["tray_info_idx"], sent["tray_type"], sent["tray_color"],
                          sent["nozzle_temp_min"], sent["nozzle_temp_max"]), (255, 254, "GFG99", "PETG", "FF0000FF", 220, 260))

    def test_set_spool_ams_slot(self):
        server.tool_set_spool({"printer": "a1", "profile_id": "GFG99", "color": "FF0000FF", "slot": "B3",
                               "idempotency_key": self.key()})
        sent = [p for p in self.link.published if p.get("print", {}).get("command") == "ams_filament_setting"][-1]["print"]
        self.assertEqual((sent["ams_id"], sent["tray_id"]), (1, 2))

    def test_orca_cad_profiles_accepted(self):
        for pid in ("GFL05", "GFL04", "GFL01", "GFL00", "GFG60", "GFB60", "GFB61", "GFL03"):
            server.tool_set_spool({"printer": "a1", "profile_id": pid, "color": "000000FF", "idempotency_key": self.key()})
        self.assertEqual(self.link.published[-2]["print"]["tray_info_idx"], "GFL03")

    def test_set_spool_rejects_bad_values(self):
        for bad in ({"profile_id": "XX", "color": "FF0000FF"}, {"profile_id": ["GFL99"], "color": "FF0000FF"},
                    {"profile_id": "GFL99", "color": "red"}, {"profile_id": "GFL99", "color": "ff0000ff"},
                    {"profile_id": "GFL99", "color": "FF0000FF", "slot": "E1"},
                    {"profile_id": "GFL99", "color": "FF0000FF", "slot": "A5"},
                    {"profile_id": "GFL99", "color": "FF0000FF", "slot": "a1"},
                    {"profile_id": "GFL99", "color": "FF0000FF", "slot": 3}):
            with self.assertRaises(server.ToolError):
                server.tool_set_spool({"printer": "a1", "idempotency_key": self.key(), **bad})
        self.assertEqual(self.link.published, [])

    def test_plate_saved_and_reported(self):
        self.assertEqual(server.plate_type("a1"), None)
        server.tool_set_plate({"printer": "a1", "plate": "Textured PEI Plate"})
        self.assertEqual(server.plate_type("a1"), "Textured PEI Plate")
        with self.assertRaises(server.ToolError):
            server.tool_set_plate({"printer": "a1", "plate": "Glass"})
        self.assertEqual(server.plate_type("a1"), "Textured PEI Plate")

    def test_ready_files_newest_first(self):
        server.library.save(1, b"old")
        server.library.save(7, b"new")
        with server.DB:
            server.DB.execute("INSERT INTO uploads VALUES (1,'old.gcode.3mf','N1','0.4','PLA',600,5.0,NULL,NULL)")
            server.DB.execute("INSERT INTO uploads VALUES (7,'new.gcode.3mf','N1','0.4','PETG',1200,9.5,'Cool Plate',NULL)")
        files = body(server.tool_ready_files({"printer": "a1"}))["files"]
        self.assertEqual([f["file_id"] for f in files], [7, 1])
        self.assertEqual(files[0]["filament"], "PETG")

    def test_delete_file_removes_it(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        server.tool_delete_file({"printer": "a1", "file_id": fid})
        self.assertEqual(body(server.tool_ready_files({"printer": "a1"}))["files"], [])
        with server.DB:
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_rename_file(self):
        fid = server.upload_file("old.gcode.3mf", make_3mf())["file_id"]
        out = body(server.tool_rename_file({"printer": "a1", "file_id": fid, "name": "my part"}))
        self.assertEqual(out["name"], "my_part.gcode.3mf")
        files = body(server.tool_ready_files({"printer": "a1"}))["files"]
        self.assertEqual(files[0]["name"], "my_part.gcode.3mf")

    def test_rename_file_rejects_bad_name(self):
        fid = server.upload_file("old.gcode.3mf", make_3mf())["file_id"]
        with self.assertRaises(server.ToolError):
            server.tool_rename_file({"printer": "a1", "file_id": fid, "name": "a/b"})

    def test_delete_unknown_file(self):
        with self.assertRaises(server.ToolError):
            server.tool_delete_file({"printer": "a1", "file_id": 999})

    def test_upload_writes_folder_not_blob(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        self.assertTrue((Path(self.tmp.name) / "files" / f"{fid}.gcode.3mf").exists())
        with server.DB:
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 1)

    def test_file_ids_continue_after_existing(self):
        with server.DB:
            server.DB.execute("INSERT INTO uploads VALUES (15,'old.gcode.3mf','N1','0.4','PLA',600,5.0,NULL,NULL)")
        server.library.save(15, b"old")
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        self.assertEqual(fid, 16)
        self.assertEqual(server.library.read(15), b"old")

    def test_migrate_blobs_moves_rows(self):
        data = make_3mf()
        with server.DB:
            server.DB.execute("INSERT INTO files VALUES (1, ?)", (data,))
            server.DB.execute("INSERT INTO uploads VALUES (1,'old.gcode.3mf','N1','0.4','PLA',600,5.0,NULL,NULL)")
            server.DB.execute("INSERT INTO uploads VALUES (2,'orphan.gcode.3mf','N1','0.4','PLA',60,1.0,NULL,NULL)")
        server.migrate_blobs()
        self.assertEqual(server.library.read(1), data)
        with server.DB:
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            self.assertEqual(server.DB.execute("SELECT file_id FROM uploads ORDER BY file_id").fetchall(), [(1,)])
        with self.assertRaises(FileNotFoundError):
            server.library.read(2)

    def test_upload_disk_error_is_english(self):
        with mock.patch.object(server.library, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(server.ToolError, "Could not save the file on the server"):
                server.upload_file("a.gcode.3mf", make_3mf())
        with server.DB:
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)

    def test_ready_files_caps_folder(self):
        server.library.setup(str(Path(self.tmp.name) / "files"))
        server.upload_file("seed.gcode.3mf", make_3mf())
        size = (Path(self.tmp.name) / "files" / "1.gcode.3mf").stat().st_size
        server.library.setup(str(Path(self.tmp.name) / "files"), cap_bytes=size * 2 + 1)
        for i in range(1, 6):
            server.upload_file(f"f{i}.gcode.3mf", make_3mf())
        files = body(server.tool_ready_files({"printer": "a1"}))["files"]
        ids = [f["file_id"] for f in files]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(ids), 2)
        with server.DB:
            self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 2)

    def test_new_upload_after_delete_is_newest(self):
        a = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        b = server.upload_file("b.gcode.3mf", make_3mf())["file_id"]
        self.assertEqual((a, b), (1, 2))
        server.tool_delete_file({"printer": "a1", "file_id": b})
        c = server.upload_file("c.gcode.3mf", make_3mf())["file_id"]
        self.assertGreater(c, b)
        files = body(server.tool_ready_files({"printer": "a1"}))["files"]
        self.assertEqual(files[0]["file_id"], c)
        self.assertNotIn(b, [f["file_id"] for f in files])

    def test_plate_mismatch(self):
        self.assertIsNone(server.plate_mismatch("a1", {"plate": None}))
        self.assertIn("say which plate", server.plate_mismatch("a1", {"plate": "Cool Plate"}))
        server.tool_set_plate({"printer": "a1", "plate": "Textured PEI Plate"})
        self.assertIn("The Textured PEI Plate plate is set", server.plate_mismatch("a1", {"plate": "Cool Plate"}))
        self.assertIsNone(server.plate_mismatch("a1", {"plate": "Textured PEI Plate"}))

    def test_old_db_gets_plate_column(self):
        path = str(Path(self.tmp.name) / "old.db")
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE uploads (file_id INTEGER PRIMARY KEY, name TEXT, model TEXT, nozzle TEXT, filament TEXT, time_s INTEGER, weight_g REAL)")
        old.execute("INSERT INTO uploads VALUES (3,'a.gcode.3mf','N1','0.4','PLA',60,1.0)")
        old.commit()
        old.close()
        server.open_db(path)
        server.open_db(path)  # second start: column already there
        self.assertIsNone(server.load_upload(3)["plate"])

    def test_print_requests_queue(self):
        url = "https://makerworld.com/en/models/12345-cube"
        rid = body(server.tool_request_print({"printer": "a1", "url": url}))["request_id"]
        pending = body(server.tool_print_requests({"printer": "a1"}))["requests"]
        self.assertEqual([(r["id"], r["url"], r["printer"]) for r in pending], [(rid, url, "a1")])
        server.tool_print_requests({"printer": "a1", "done": rid})
        self.assertEqual(body(server.tool_print_requests({"printer": "a1"}))["requests"], [])

    def test_request_print_copies(self):
        url = "https://makerworld.com/en/models/1-cube"
        rid = body(server.tool_request_print({"printer": "a1", "url": url, "copies": 6}))["request_id"]
        req = body(server.tool_print_requests({"printer": "a1"}))["requests"][0]
        self.assertEqual((req["id"], req["copies"]), (rid, 6))
        server.tool_print_requests({"printer": "a1", "done": rid})
        server.tool_request_print({"printer": "a1", "url": url})
        self.assertEqual(body(server.tool_print_requests({"printer": "a1"}))["requests"][0]["copies"], 1)
        for bad in (0, 51, "3"):
            with self.assertRaises(server.ToolError):
                server.tool_request_print({"printer": "a1", "url": url, "copies": bad})

    def test_request_flow_and_cancel(self):
        server.CFG["orca_wake_url"] = None
        with server.DB:
            server.DB.execute("INSERT INTO uploads VALUES (9,'cube.gcode.3mf','N1','0.4','PLA',60,1.0,NULL,NULL)")
        url = "https://makerworld.com/en/models/1-cube"
        rid = body(server.tool_request_print({"printer": "a1", "url": url}))["request_id"]
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "working"})
        with self.assertRaises(server.ToolError):  # ready needs an uploaded file
            server.tool_print_requests({"printer": "a1", "id": rid, "status": "ready", "file_id": 404})
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "ready", "file_id": 9})
        req = body(server.tool_print_requests({"printer": "a1"}))["requests"][0]
        self.assertEqual((req["status"], req["file_id"]), ("ready", 9))
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "started"})
        self.assertEqual(body(server.tool_print_requests({"printer": "a1"}))["requests"], [])
        # Cancelled while Orca-Cad works: its next update fails and the request leaves the queue.
        rid = body(server.tool_request_print({"printer": "a1", "url": url}))["request_id"]
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "working"})
        server.tool_cancel_request({"printer": "a1", "request_id": rid})
        with self.assertRaisesRegex(server.ToolError, "annullata"):
            server.tool_print_requests({"printer": "a1", "id": rid, "status": "ready", "file_id": 9})
        self.assertEqual(body(server.tool_print_requests({"printer": "a1"}))["requests"], [])
        with self.assertRaises(server.ToolError):
            server.tool_cancel_request({"printer": "a1", "request_id": rid})

    def test_multi_plate_request_offers_plates_in_order(self):
        with server.DB:
            for fid in (21, 22, 23):
                server.DB.execute("INSERT INTO uploads VALUES (?,'p.gcode.3mf','N1','0.4','PLA',60,1.0,NULL,NULL)", (fid,))
        rid = body(server.tool_request_print({"printer": "a1", "url": "https://makerworld.com/en/models/2-box"}))["request_id"]
        with self.assertRaises(server.ToolError):
            server.tool_print_requests({"printer": "a1", "id": rid, "status": "ready", "file_ids": [21, 404]})
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "ready", "file_ids": [21, 22, 23]})
        seen = []
        for _ in range(3):
            req = body(server.tool_print_requests({"printer": "a1"}))["requests"][0]
            seen.append((req["file_id"], req["plate_index"], req["plates"]))
            server.tool_print_requests({"printer": "a1", "id": rid, "status": "started"})
        self.assertEqual([x[:2] for x in seen], [(21, 0), (22, 1), (23, 2)])
        self.assertEqual(seen[0][2], [21, 22, 23])
        self.assertEqual(body(server.tool_print_requests({"printer": "a1"}))["requests"], [])

    def test_wake_orca_posts_request_id(self):
        sent = []

        class Reply:
            def close(self):
                pass

        real = server.urllib.request.urlopen
        server.urllib.request.urlopen = lambda req, timeout: sent.append(req) or Reply()
        self.addCleanup(setattr, server.urllib.request, "urlopen", real)
        server.CFG.update(orca_wake_url="http://mini:9/wake", orca_wake_token="t" * 32)
        self.addCleanup(server.CFG.update, orca_wake_url=None)
        server.wake_orca(5).join()
        self.assertEqual((sent[0].full_url, json.loads(sent[0].data), sent[0].get_header("Authorization")),
                         ("http://mini:9/wake", {"request_id": 5}, "Bearer " + "t" * 32))

    def test_request_print_only_makerworld(self):
        for bad in ("http://makerworld.com/x", "https://makerworld.com.evil.io/x", "https://evil.io/makerworld.com/x",
                    "javascript:alert(1)", "https://makerworld.com/a b", 42):
            with self.assertRaises(server.ToolError):
                server.tool_request_print({"printer": "a1", "url": bad})

    def test_new_tools_registered(self):
        for name in ("set_nozzle", "set_spool", "set_plate", "delete_file", "rename_file", "ready_files", "request_print", "print_requests",
                     "queue_add", "queue_remove", "queue_list", "slice_request"):
            self.assertIn(name, server.TOOLS)


if __name__ == "__main__":
    unittest.main()


class ReadFilamentTest(unittest.TestCase):
    def test_never_while_that_ams_feeds_the_print(self):
        from unittest import mock
        with mock.patch.object(server, "read_status", return_value={"state": "RUNNING", "tray_now": "1"}), \
             mock.patch.object(server, "run_direct", return_value="sent") as run:
            with self.assertRaises(server.ToolError):
                server.tool_read_filament({"printer": "a1", "slot": "A3"})
            self.assertEqual(server.tool_read_filament({"printer": "a1", "slot": "B1"}), "sent")
            self.assertEqual(run.call_args[0][3], {"slot": "B1"})
        with mock.patch.object(server, "read_status", return_value={"state": "FINISH", "tray_now": "1"}), \
             mock.patch.object(server, "run_direct", return_value="sent"):
            self.assertEqual(server.tool_read_filament({"printer": "a1", "slot": "A2"}), "sent")
        with self.assertRaises(server.ToolError):
            server.tool_read_filament({"printer": "a1", "slot": "E1"})
