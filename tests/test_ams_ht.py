"""An AMS HT (unit id 128) is drawn as one slot, not as a second "A"."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
from pygmy import onboard, server  # noqa: E402

UNITS = [
    {"id": "0", "info": "50001003", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF",
                                              "tray_sub_brands": "PLA Basic"}]},
    {"id": "1", "info": "51001103", "tray": []},
    {"id": "128", "info": "10002004", "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "DCDCDCFF",
                                                "tray_sub_brands": "", "tag_uid": "0000000000000000"}]},
]


class AmsHt(unittest.TestCase):
    def test_ht_is_one_slot_named_ht1(self):
        units = onboard.ams_units(UNITS, tray_now=512, exist_bits="100ff")
        self.assertEqual([u["unit"] for u in units], ["A", "B", "HT"])
        ht = units[2]
        self.assertEqual(len(ht["slots"]), 1)
        self.assertEqual(ht["slots"][0]["slot"], "HT1")
        self.assertFalse(ht["slots"][0]["empty"])
        self.assertEqual(ht["slots"][0]["material"], "ABS")
        self.assertTrue(ht["slots"][0]["active"])
        self.assertEqual(len(units[0]["slots"]), 4)

    def test_slot_names_and_indexes(self):
        self.assertEqual(server.slot_index("HT1"), 512)
        self.assertEqual(server.slot_index("A1"), 0)
        self.assertEqual(onboard.unit_letter(128), "HT")


if __name__ == "__main__":
    unittest.main()
