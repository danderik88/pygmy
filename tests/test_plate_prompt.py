import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pygmy import server  # noqa: E402

APP = (Path(__file__).resolve().parents[1] / "src" / "pygmy" / "app.html").read_text()


class PlatePrompt(unittest.TestCase):
    """A print refused for the plate must say so and ask which plate is on the printer (the user, 2026-11-25)."""

    def test_unknown_plate_is_said_in_english(self):
        with unittest.mock.patch.object(server, "plate_type", lambda a: None), \
                unittest.mock.patch.object(server, "link_of", lambda a: object()):
            msg = server.plate_mismatch("alpha", {"plate": "Cool Plate"})
        self.assertEqual(msg, "This file needs the Cool Plate plate: say which plate is on the printer")

    def test_start_refusal_opens_the_plate_picker_and_retries(self):
        self.assertIn("needs the .* plate|plate is set", APP)
        self.assertIn("askPlate({ note: e.message }, () => pnStartPrint(", APP)

    def test_unknown_refusals_show_their_reason_not_a_generic_text(self):
        self.assertNotIn("Command refused by Pygmy", APP)

    def test_a_failed_slice_is_shown_as_an_error(self):
        self.assertIn('pnProgress(enNote(r.note || "Slicing failed"), true)', APP)
        self.assertIn(".note.bad { font-size: 14px; font-weight: 600; color: var(--bad); }", APP)

    def test_blocked_reasons_are_english(self):
        self.assertEqual(server.blocked_reason("start", {"connected": False}), "The printer is not connected")


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
