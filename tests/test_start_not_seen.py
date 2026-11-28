"""A start the printer never ran says what the printer did. Run: python3 -m unittest discover -s tests"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pygmy import server  # noqa: E402

UPLOAD = {"name": "air_test.gcode.3mf"}


class FakeLink:
    def __init__(self, start_reply=None):
        self.start_reply = start_reply


def note(raw):
    with mock.patch.object(server, "read_status", return_value=raw), mock.patch.object(server, "load_upload", return_value=UPLOAD):
        return server.start_not_seen_note("alpha", {"file_id": 7})


def note_with_reply(raw, start_reply):
    link = FakeLink(start_reply)
    with mock.patch.object(server, "read_status", return_value=raw), \
            mock.patch.object(server, "load_upload", return_value=UPLOAD), \
            mock.patch.object(server, "link_of", return_value=link):
        return server.start_not_seen_note("alpha", {"file_id": 7})


class StartNotSeenNote(unittest.TestCase):
    def test_job_received_but_file_not_loaded(self):
        text = note({"state": "IDLE", "subtask_name": "air_test"})
        self.assertIn("received the job", text)
        self.assertIn("rejected the file", text)

    def test_command_ignored(self):
        text = note({"state": "FINISH", "subtask_name": "old_job"})
        self.assertEqual(text, "the printer is still FINISH: it did not start the job")

    def test_print_error_is_named(self):
        self.assertIn("print error 50348044", note({"state": "IDLE", "subtask_name": "air_test", "print_error": 50348044}))

    def test_no_note_while_the_printer_is_busy(self):
        self.assertEqual(note({"state": "RUNNING", "subtask_name": "air_test"}), "")

    def test_no_note_when_the_status_cannot_be_read(self):
        with mock.patch.object(server, "read_status", side_effect=server.ToolError("offline")):
            self.assertEqual(server.start_not_seen_note("alpha", {"file_id": 7}), "")

    def test_printer_answered_fail_without_reason(self):
        text = note_with_reply({"state": "IDLE", "subtask_name": "air_test"}, {"result": "fail", "at": time.time()})
        self.assertIn('answered the start with result "fail"', text)
        self.assertIn("no reason", text)

    def test_printer_answered_fail_with_reason(self):
        text = note_with_reply({"state": "IDLE", "subtask_name": "air_test"}, {"result": "fail", "reason": "bad mapping", "at": time.time()})
        self.assertIn("(reason: bad mapping)", text)

    def test_stale_reply_leaves_note_unchanged(self):
        base = note({"state": "IDLE", "subtask_name": "air_test"})
        text = note_with_reply({"state": "IDLE", "subtask_name": "air_test"}, {"result": "fail", "at": time.time() - 600})
        self.assertEqual(text, base)

    def test_no_reply_leaves_note_unchanged(self):
        base = note({"state": "IDLE", "subtask_name": "air_test"})
        text = note_with_reply({"state": "IDLE", "subtask_name": "air_test"}, None)
        self.assertEqual(text, base)


START_UPLOAD = {"name": "air_test.gcode.3mf", "weight_g": 10, "filament": "PLA", "nozzle": 0.4, "time_s": 600}


def prepare_summary(ams_units):
    captured = {}

    def fake_issue_token(alias, action, params, summary, photo_url=None):
        captured["summary"] = summary
        return []

    patches = {
        "printer_id": mock.DEFAULT, "require_support": mock.DEFAULT, "assert_unlocked": mock.DEFAULT,
        "load_upload": START_UPLOAD, "upload_color": None, "blocked_reason": None, "check_compatible": None,
        "plate_mismatch": None, "snapshot_jpeg": b"jpg", "filament_note": "", "store_photo": "url",
        "image_content": {}, "load_profile": {},
    }
    # each value is what the patched function returns (a bare dict would replace the function itself)
    patches = {k: v if v is mock.DEFAULT else mock.Mock(return_value=v) for k, v in patches.items()}
    with mock.patch.multiple(server, **patches), \
            mock.patch.object(server, "read_status", return_value={"state": "IDLE", "ams_units": ams_units}), \
            mock.patch.object(server, "issue_token", side_effect=fake_issue_token):
        server.tool_prepare_print_start({"printer": "alpha", "file_id": 7})
    return captured["summary"]


class PrepareDryingNote(unittest.TestCase):
    def test_drying_unit_is_named(self):
        text = prepare_summary([{"unit": "A", "drying": False}, {"unit": "HT", "drying": True}])
        self.assertIn("AMS HT is drying", text)
        self.assertNotIn("AMS A is drying", text)

    def test_no_note_without_drying(self):
        self.assertNotIn("is drying", prepare_summary([{"unit": "A", "drying": False}]))


def wait(start_reply, timeout_s):
    link = FakeLink(start_reply)
    with mock.patch.object(server, "read_status", return_value={"connected": True, "state": "IDLE", "subtask_name": "air_test"}), \
            mock.patch.object(server, "load_upload", return_value=UPLOAD), \
            mock.patch.object(server, "load_profile", return_value={"start_timeout_s": timeout_s}), \
            mock.patch.object(server, "link_of", return_value=link):
        began = time.monotonic()
        result = server.wait_for_outcome("alpha", "start", {"file_id": 7})
        elapsed = time.monotonic() - began
        return result, elapsed, server.start_not_seen_note("alpha", {"file_id": 7})


class StartFailEndsWait(unittest.TestCase):
    def test_fail_reply_ends_the_wait(self):
        (state, seen), elapsed, detail = wait({"at": time.time(), "result": "fail"}, 30)
        self.assertFalse(seen)
        self.assertLess(elapsed, 3)
        self.assertIn('answered the start with result "fail"', detail)

    def test_success_reply_keeps_waiting(self):
        (state, seen), elapsed, _ = wait({"at": time.time(), "result": "success"}, 1)
        self.assertFalse(seen)
        self.assertGreaterEqual(elapsed, 1)

    def test_old_fail_reply_keeps_waiting(self):
        (_, _), elapsed, _ = wait({"at": time.time() - 600, "result": "fail"}, 1)
        self.assertGreaterEqual(elapsed, 1)


if __name__ == "__main__":
    unittest.main()
