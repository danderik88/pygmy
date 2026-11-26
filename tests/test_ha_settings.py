import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "src" / "pygmy" / "app.html").read_text()


class HomeAssistantSettings(unittest.TestCase):
    """The Home Assistant section says how to get the entities (MQTT) as well as the camera."""

    def test_section_is_always_shown_and_names_the_mqtt_settings(self):
        self.assertIn('sec("ha", "Home Assistant", "", camOptions())', APP)
        self.assertNotIn('camUrls ? sec("ha"', APP)
        for name in ("BFF_HA_MQTT_HOST", "BFF_HA_MQTT_PORT", "BFF_HA_MQTT_USER", "BFF_HA_MQTT_PASSWORD", "pygmy_&lt;printer&gt;_state"):
            self.assertIn(name, APP)

    def test_env_template_and_readme_document_the_bridge(self):
        env = (ROOT / "_template.env").read_text()
        readme = (ROOT / "README.md").read_text()
        # the public copy writes PYGMY_ where this repo says BFF_: test the part both share
        for name in ("HA_MQTT_HOST", "HA_MQTT_USER", "HA_MQTT_PASSWORD"):
            self.assertIn(name, env)
            self.assertIn(name, readme)


if __name__ == "__main__":
    unittest.main()
