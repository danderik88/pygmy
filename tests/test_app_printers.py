import re
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "pygmy" / "app.html").read_text()


class AppPrinters(unittest.TestCase):
    def test_add_sheet(self):
        for text in ("+ Add printer", "Enter IP", "Next", "Not supported yet", "New printer found",
                     "On the printer: Settings → Network → Access code"):
            self.assertIn(text, PAGE)
        self.assertIn("P1S enclosure kit fitted", PAGE)  # the user: ask it with a tick, only for a P1P
        self.assertIn('apRow.model === "C11"', PAGE)
        code = re.search(r'<input [^>]*id="apcode"[^>]*>', PAGE).group(0)
        for attr in ('maxlength="8"', 'autocomplete="off"', 'type="text"', 'autocapitalize="off"', 'autocorrect="off"'):
            self.assertIn(attr, code)
        self.assertNotIn("inputmode", code)  # Alpha's code has letters (the user, 2026-11-12)

    def test_plate_question_gets_a_list(self):
        self.assertIn("Which plate is on the printer?", PAGE)
        self.assertIn('if (/plate/i.test(r.note || "")) return askPlate(r);', PAGE)
        self.assertIn('rpc("set_plate", printer ? { plate, printer } : { plate })', PAGE)

    def test_ams_drawn_like_handy(self):
        for text in ('id="amsmini"', 'id="amscard"', "AMS-${u.unit}", "s.ams_units", '"Empty"', "in use"):
            self.assertIn(text, PAGE)

    def test_printers_synced_at_start(self):
        self.assertIn("const firstLoad = () => syncPrinters().finally(() => {", PAGE)
        self.assertIn("HEAD.headFor(info.model)", PAGE)

    def test_names_from_bff(self):
        self.assertIn("names", PAGE)  # printer_info.names replaces the alias in upper case

    def test_manage_rows(self):
        self.assertIn("Change access code", PAGE)
        self.assertIn("Remove", PAGE)

    def test_select_mode(self):
        # the user 2026-11-12: a Select button, tick printers, remove them.
        self.assertIn(">Select<", PAGE)
        self.assertIn('type="checkbox" class="psel"', PAGE)
        self.assertIn("remove_printer", PAGE)
        self.assertNotIn('class="acts manage"', PAGE)  # no per-row buttons outside select mode

    def test_uses_app_tools(self):
        for tool in ("discover_printers", "add_printer", "set_access_code", "remove_printer"):
            self.assertIn(f'rpc("{tool}"', PAGE)

    def test_code_never_kept(self):
        # The access code goes to BFF once and is never stored in the browser.
        self.assertNotRegex(PAGE, r"localStorage\.setItem\([^)]*(code|access)", )

    def test_setup_hint(self):
        # The Add printer form must say how to put the printer in LAN-only/Developer Mode.
        self.assertIn("turn on LAN-only mode and Developer Mode. The access code is on the same screen", PAGE)

    def test_add_printer_errors_shown_raw(self):
        # The Add sheet (and change-code) show the English message BFF sends, not a translated fallback.
        self.assertIn('rpc("add_printer", args, false)', PAGE)
        self.assertIn('rpc("set_access_code", { printer: a, access_code: input.value }, false)', PAGE)


if __name__ == "__main__":
    unittest.main()
