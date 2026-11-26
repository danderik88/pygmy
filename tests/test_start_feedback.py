"""Starting a print from any page says what is happening and keeps the reason on screen. Run: python3 -m unittest discover -s tests"""
import unittest
from pathlib import Path

PAGE = (Path(__file__).parent.parent / "src" / "pygmy" / "app.html").read_text()


class StartFeedback(unittest.TestCase):
    def test_waiting_for_the_printer_is_visible_on_the_files_page(self):
        self.assertIn("Sending the file to the printer… waiting for it to start (up to 3 minutes)", PAGE)
        self.assertIn("function toast(text, ms = 4000)", PAGE)

    def test_a_refusal_stays_on_screen_long_enough_to_read(self):
        self.assertIn("toast(e.message, 12000)", PAGE)

    def test_busy_note_gives_the_reason(self):
        self.assertIn("Can't start now:", PAGE)


if __name__ == "__main__":
    unittest.main()
