import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SpaghettiInDocker(unittest.TestCase):
    """Obico runs in its own container: without a listener it can reach, the detector never started (found 2026-11-26)."""

    def test_compose_starts_the_frame_listener_and_points_obico_at_the_host(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('BFF_LAN_BIND: "0.0.0.0"', compose)
        self.assertIn('BFF_OBICO_FRAME_BASE: "http://host.docker.internal:8766"', compose)
        self.assertIn("host.docker.internal:host-gateway", compose)

    def test_server_uses_the_explicit_frame_base(self):
        server = (ROOT / "src" / "pygmy" / "server.py").read_text()
        self.assertIn('obico_frame_base=env.get("BFF_OBICO_FRAME_BASE") or', server)

    def test_the_app_says_when_the_detector_is_not_running(self):
        server = (ROOT / "src" / "pygmy" / "server.py").read_text()
        app = (ROOT / "src" / "pygmy" / "app.html").read_text()
        self.assertIn('result["spaghetti_engine"] = SPAGHETTI_RUNNING', server)
        self.assertIn("spaghetti detection is NOT running", server)
        self.assertIn("extra.spaghetti_engine === false", app)
        self.assertIn("Spaghetti detection is not running on this server", app)


if __name__ == "__main__":
    unittest.main()
