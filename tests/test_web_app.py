"""The phone page and its camera frame, served by bff-mcp."""
import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from pygmy import server  # noqa: E402

TOKEN = "t" * 40
JPEG = b"\xff\xd8frame"


class WebApp(unittest.TestCase):
    def setUp(self):
        server.CFG.update(printers={"a1": 1}, tokens={"iphone": TOKEN}, control=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, None, b""

    def test_app_page_served_without_token(self):
        code, ctype, body = self.get("/app")
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertNotIn(TOKEN.encode(), body)

    def test_camera_needs_token(self):
        with mock.patch.object(server, "capture_jpeg", return_value=JPEG) as cap:
            self.assertEqual(self.get("/camera/a1")[0], 401)
            self.assertEqual(self.get("/camera/a1", "x" * 40)[0], 401)
        cap.assert_not_called()

    def test_camera_unknown_alias_404(self):
        with mock.patch.object(server, "capture_jpeg", return_value=JPEG) as cap:
            self.assertEqual(self.get("/camera/p1s", TOKEN)[0], 404)
        cap.assert_not_called()

    def test_stream_needs_its_token(self):
        self.addCleanup(server.CFG.update, stream_token=server.CFG.get("stream_token", ""))
        server.CFG["stream_token"] = "x" * 20
        self.assertEqual(self.get("/stream/a1/wrongtoken")[0], 401)
        self.assertEqual(self.get("/stream/nosuchprinter/" + "x" * 20)[0], 404)

    def test_lan_listener_serves_only_the_stream(self):
        lan = ThreadingHTTPServer(("127.0.0.1", 0), server.StreamOnlyHandler)
        threading.Thread(target=lan.serve_forever, daemon=True).start()
        self.addCleanup(lan.server_close)
        self.addCleanup(lan.shutdown)
        base = f"http://127.0.0.1:{lan.server_address[1]}"
        for path in ("/app", "/camera/a1", "/mcp", "/stream/a1/wrongtoken"):
            req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {TOKEN}"})
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=5)
            self.assertIn(err.exception.code, (401, 404), path)
            self.assertNotEqual(err.exception.code, 200)

    def test_camera_returns_jpeg_without_light(self):
        with mock.patch.object(server, "capture_jpeg", return_value=JPEG) as cap, \
                mock.patch.object(server, "with_light") as light:
            code, ctype, body = self.get("/camera/a1", TOKEN)
        self.assertEqual((code, ctype, body), (200, "image/jpeg", JPEG))
        cap.assert_called_once_with(1)
        light.assert_not_called()

    def test_camera_error_is_502(self):
        with mock.patch.object(server, "capture_jpeg", side_effect=server.ToolError("giù")):
            self.assertEqual(self.get("/camera/a1", TOKEN)[0], 502)

    def test_other_paths_still_refused(self):
        self.assertEqual(self.get("/etc/passwd")[0], 401)
        self.assertEqual(self.get("/app/../x")[0], 401)

    def test_status_calls_not_logged(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "status", "arguments": {"printer": "a1"}}}).encode()
        req = urllib.request.Request(self.base + "/", data=body, method="POST",
                                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(server, "status_of", return_value={"state": "IDLE"}), \
                mock.patch.object(server, "DB", None):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        self.assertNotIn("tools/call status", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class Compression(WebApp):
    """A 5G link relayed by Tailscale stalled on the 200 KB page (2026-11-23): text answers go out gzipped."""
    def fetch(self, path, accept):
        req = urllib.request.Request(self.base + path)
        if accept:
            req.add_header("Accept-Encoding", accept)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.headers.get("Content-Encoding"), resp.read()

    def test_the_page_is_gzipped_when_the_client_accepts_it(self):
        import gzip
        plain = self.fetch("/app", None)
        zipped = self.fetch("/app", "gzip")
        self.assertIsNone(plain[0])
        self.assertEqual(zipped[0], "gzip")
        self.assertEqual(gzip.decompress(zipped[1]), plain[1])
        self.assertLess(len(zipped[1]), len(plain[1]) / 2)

    def test_images_are_not_recompressed(self):
        self.assertIsNone(self.fetch("/icon-192.png", "gzip")[0])

    def test_the_json_answers_of_the_app_are_gzipped_too(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        req = urllib.request.Request(self.base + "/", data=body, method="POST", headers={
            "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
