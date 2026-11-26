"""A start the printer never ran says what the printer did. Run: python3 -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pygmy import server  # noqa: E402

UPLOAD = {"name": "air_test.gcode.3mf"}


def note(raw):
    with mock.patch.object(server, "read_status", return_value=raw), mock.patch.object(server, "load_upload", return_value=UPLOAD):
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


if __name__ == "__main__":
    unittest.main()
