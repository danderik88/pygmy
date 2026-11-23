import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pygmy import server  # noqa: E402


class DefaultProcess(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        patches = [mock.patch.object(server, "DB", db), mock.patch.object(server, "printer_id", lambda a: a["printer"])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.user = {"processes": {"0.20mm Standard @Creality E3K 0.6": {}, "Other": {}}}

    def save(self, process):
        return json.loads(server.tool_set_print_option(
            {"printer": "e3k", "option": "bff_default_process", "process": process})[0]["text"])

    def test_default_is_used_when_nothing_was_picked(self):
        self.save("0.20mm Standard @Creality E3K 0.6")
        choices = {}
        server.apply_default_process("e3k", self.user, choices)
        self.assertEqual(choices["process"], "0.20mm Standard @Creality E3K 0.6")

    def test_a_pick_in_print_now_wins(self):
        self.save("0.20mm Standard @Creality E3K 0.6")
        choices = {"process": "Other"}
        server.apply_default_process("e3k", self.user, choices)
        self.assertEqual(choices["process"], "Other")

    def test_a_default_that_is_gone_falls_back_to_the_built_in_profile(self):
        self.save("Deleted preset")
        choices = {}
        server.apply_default_process("e3k", self.user, choices)
        self.assertNotIn("process", choices)

    def test_it_is_per_printer_and_can_be_cleared(self):
        self.save("Other")
        choices = {}
        server.apply_default_process("a1", self.user, choices)
        self.assertNotIn("process", choices)
        self.save("")
        server.apply_default_process("e3k", self.user, choices)
        self.assertNotIn("process", choices)


if __name__ == "__main__":
    unittest.main()
