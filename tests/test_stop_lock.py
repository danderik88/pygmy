import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pygmy import server  # noqa: E402


class LateStop(unittest.TestCase):
    """Klipper stops only after its start routine: the Stop comes back 'unknown' and must not block the next print."""

    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE locks (printer TEXT PRIMARY KEY, key TEXT)")
        db.execute("CREATE TABLE commands (key TEXT PRIMARY KEY, printer TEXT, action TEXT, params TEXT, outcome TEXT, detail TEXT)")
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT)")
        self.db = db
        self.state ={"connected": True, "state": "FAILED"}
        self.link = mock.Mock(spec=server.MoonrakerLink)
        for p in (mock.patch.object(server, "DB", db), mock.patch.object(server, "read_status", lambda a: self.state),
                  mock.patch.object(server, "printer_id", lambda a: a["printer"]),
                  mock.patch.object(server, "link_of", lambda a: self.link)):
            p.start()
            self.addCleanup(p.stop)

    def lock(self, action):
        self.db.execute("INSERT INTO commands VALUES ('k1','e3k',?,'{}','unknown','x')", (action,))
        self.db.execute("INSERT INTO locks VALUES ('e3k','k1')")

    def locked(self):
        return self.db.execute("SELECT 1 FROM locks WHERE printer='e3k'").fetchone() is not None

    def test_a_stop_that_took_effect_late_releases_the_lock_for_the_next_command(self):
        self.lock("stop")
        server.assert_unlocked("e3k", "start")
        self.assertFalse(self.locked())

    def test_an_unknown_start_still_needs_a_recheck(self):
        self.lock("start")
        with self.assertRaises(server.ToolError):
            server.assert_unlocked("e3k", "start")
        self.assertTrue(self.locked())

    def test_a_stop_on_a_disconnected_printer_keeps_the_lock(self):
        self.lock("stop")
        self.state = {"connected": False}
        with self.assertRaises(server.ToolError):
            server.assert_unlocked("e3k", "start")
        self.assertTrue(self.locked())

    def test_a_bambu_printer_keeps_its_lock_after_an_unknown_stop(self):
        self.link = object()
        self.lock("stop")
        with self.assertRaises(server.ToolError):
            server.assert_unlocked("e3k", "start")
        self.assertTrue(self.locked())

    def test_the_screen_does_not_call_the_printer_busy_for_a_stop_that_settled(self):
        self.lock("stop")
        with mock.patch.object(server, "blocked_reason", lambda c, raw: None), \
                mock.patch.object(server, "load_profile", lambda a: {}), \
                mock.patch.object(server, "printer_filaments", lambda a, r: []), \
                mock.patch.object(server, "app_names_models", lambda: ({}, {})), \
                mock.patch.object(server, "flow_cali_shown", lambda a, r: False), \
                mock.patch.object(server, "profile_of", lambda a: None), \
                mock.patch.dict(server.CFG, {"printers": {"e3k": "x"}}):
            info = server.tool_printer_info({"printer": "e3k"})
        self.assertIn('"free": true', info[0]["text"])
        self.assertFalse(self.locked())

    def test_recheck_still_works_by_hand(self):
        self.lock("start")
        self.state = {"connected": True, "state": "RUNNING"}
        reply = server.tool_recheck({"printer": "e3k"})
        self.assertIn("confirmed", reply[0]["text"])
        self.assertFalse(self.locked())


if __name__ == "__main__":
    unittest.main()
