"""One switch per light: snapshot entries, per-light commands and the unchanged single-light tile."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
from pygmy import server  # noqa: E402
from moonraker_link import MoonrakerLink  # noqa: E402
from printer_link import to_snapshot  # noqa: E402
import ha_mqtt  # noqa: E402

PAGE = (ROOT / "src" / "pygmy" / "app.html").read_text()


class SnapshotLights(unittest.TestCase):
    def test_klipper_lights_have_name_label_and_state(self):
        link = MoonrakerLink("k", "h")
        link.lights = ["output_pin caselight", "neopixel chamber_led"]
        link.raw = {"output_pin caselight": {"value": 0},
                    "neopixel chamber_led": {"color_data": [[255, 0, 0, 0]]}}
        link.last_report = link.clock()
        snap = link.snapshot()
        self.assertEqual(snap["lights"], [
            {"name": "output_pin caselight", "label": "Caselight", "on": False},
            {"name": "neopixel chamber_led", "label": "Chamber led", "on": True}])
        self.assertTrue(snap["chamber_light"])  # legacy field kept

    def test_bambu_work_light_node_is_not_listed(self):
        # the P2S reports a work_light node that lights nothing; the lamps answer to chamber_light
        raw = {"lights_report": [{"node": "chamber_light", "mode": "on"},
                                 {"node": "work_light", "mode": "off"}]}
        snap = to_snapshot(raw)
        self.assertEqual(snap["lights"], [{"name": "chamber_light", "label": "Chamber light", "on": True}])
        self.assertTrue(snap["chamber_light"])

    def test_bambu_without_report_keeps_an_unknown_chamber_light(self):
        snap = to_snapshot({})
        self.assertEqual(snap["lights"], [{"name": "chamber_light", "label": "Chamber light", "on": None}])
        self.assertIsNone(snap["chamber_light"])


class PerLightCommand(unittest.TestCase):
    def link(self):
        class Fake:
            sent = []

            def snapshot(self):
                return {"lights": [{"name": "output_pin caselight", "label": "Caselight", "on": True},
                                   {"name": "neopixel chamber_led", "label": "Chamber led", "on": False}]}

            def command(self, action, **params):
                self.sent.append((action, params))
                return True

        return Fake()

    def test_valid_light_is_routed(self):
        link = self.link()
        with mock.patch.object(server, "link_of", return_value=link), \
                mock.patch.object(server, "alias_of", return_value="k"):
            server.set_light(1, True, "output_pin caselight")
        self.assertEqual(link.sent, [("light", {"on": True, "light": "output_pin caselight"})])

    def test_unknown_light_is_refused_before_sending(self):
        link = self.link()
        with mock.patch.object(server, "link_of", return_value=link), \
                mock.patch.object(server, "alias_of", return_value="k"):
            with self.assertRaises(server.ToolError) as err:
                server.set_light(1, True, "output_pin beeper")
        self.assertIn("output_pin beeper", str(err.exception))
        self.assertEqual(link.sent, [])


class SingleLightTileUnchanged(unittest.TestCase):
    def test_tile_still_toggles_the_one_light(self):
        self.assertIn('id="light"', PAGE)
        self.assertIn('rpc("set_light", { on })', PAGE)
        self.assertIn("lights.length > 1", PAGE)
        self.assertIn('id="lights"', PAGE)
        self.assertIn('rpc("set_light", { on, light:', PAGE)


class HomeAssistantLights(unittest.TestCase):
    def test_one_entity_per_light_only_when_several(self):
        single = dict(ha_mqtt.discovery("a1"))
        self.assertIn("homeassistant/light/pygmy_a1/light/config", single)

        lights = [{"name": "chamber_light", "label": "Chamber light", "on": True},
                  {"name": "work_light", "label": "Work light", "on": False}]
        multi = dict(ha_mqtt.discovery("a1", lights=lights))
        self.assertNotIn("homeassistant/light/pygmy_a1/light/config", multi)
        self.assertIn("homeassistant/light/pygmy_a1/light_chamber_light/config", multi)
        self.assertEqual(multi["homeassistant/light/pygmy_a1/light_work_light/config"]["unique_id"],
                         "pygmy_a1_light_work_light")


if __name__ == "__main__":
    unittest.main()
