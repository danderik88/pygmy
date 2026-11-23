"""BFF's own notifications: alert logic, senders, notifier."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pygmy.notify import Presence, alerts  # noqa: E402


class Alerts(unittest.TestCase):
    def test_no_alert_on_first_status(self):
        assert alerts(None, {"state": "FAILED", "end_reason": "failed", "alarm": "x"}) == []

    def test_finished_and_attention_kinds(self):
        assert [k for k, _ in alerts({"state": "RUNNING"}, {"state": "FINISH"})] == ["finished"]
        assert [k for k, _ in alerts({"state": "RUNNING"}, {"state": "PAUSE", "alarm": "Filament ran out"})] == ["attention"]

    def test_user_pause_is_silent(self):
        assert alerts({"state": "RUNNING"}, {"state": "PAUSE"}) == []

    def test_spaghetti_is_a_channel_alert(self):
        # the user 2026-11-15: BFF notifications on, the spaghetti warning must reach the phone from BFF
        out = alerts({"state": "RUNNING"}, {"state": "RUNNING", "spaghetti": "warning", "file": "cube"})
        assert [k for k, _ in out] == ["spaghetti"], out
        assert "spaghetti" in out[0][1] and "camera" in out[0][1]

    def test_spaghetti_alerts_once_while_it_stays_bad(self):
        assert alerts({"state": "RUNNING", "spaghetti": "warning"}, {"state": "RUNNING", "spaghetti": "failure"}) == []

    def test_spaghetti_paused_says_so(self):
        out = alerts({"state": "RUNNING"}, {"state": "RUNNING", "spaghetti": "failure",
                                            "spaghetti_paused": True, "file": "cube"})
        assert [k for k, _ in out] == ["spaghetti"], out
        assert "paused" in out[0][1] and "camera" in out[0][1]

    def test_low_spool_once_per_print(self):
        w = set()
        run = lambda r: {"state": "RUNNING", "ams": {"A2": {"remain": r, "name": "PLA Basic"}}}
        seq = [k for a, b in [(12, 9), (9, 5), (5, 3)] for k, _ in alerts(run(a), run(b), warned=w)]
        assert seq == ["low_spool"]
        assert "A2" in alerts(run(12), run(9), warned=set())[0][1]

    def test_low_spool_resets_after_print(self):
        w = set()
        run = lambda r, s="RUNNING": {"state": s, "ams": {"A2": {"remain": r}}}
        alerts(run(12), run(9), warned=w)
        alerts(run(9), run(9, "FINISH"), warned=w)
        assert [k for k, _ in alerts(run(12), run(8), warned=w)] == ["low_spool"]

    def test_low_spool_untagged_silent(self):
        run = lambda r: {"state": "RUNNING", "ams": {"A1": {"remain": r}}}
        assert alerts(run(None), run(None), warned=set()) == []

    def test_low_spool_not_when_idle(self):
        idle = {"state": "IDLE", "ams": {"A1": {"remain": 5}}}
        assert alerts(idle, idle, warned=set()) == []

    def test_short_drop_is_silent(self):
        p = Presence()
        p.update(True, 0)
        assert [p.update(False, t) for t in (10, 100)] + [p.update(True, 110)] == [None, None, None]

    def test_offline_then_online(self):
        p = Presence()
        p.update(True, 0)
        p.update(False, 1)
        assert p.update(False, 122) == "offline" and p.update(False, 200) is None
        assert p.update(True, 210) == "online"


import json  # noqa: E402
import sqlite3  # noqa: E402
from unittest import mock  # noqa: E402

from pygmy import notify  # noqa: E402


def memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
    return db


class Gone(Exception):
    def __init__(self, code):
        super().__init__(f"push {code}")
        self.response = mock.Mock(status_code=code)


class Channels(unittest.TestCase):
    def test_vapid_stable(self):
        db = memdb()
        key = notify.vapid_public(db)
        self.assertEqual(key, notify.vapid_public(db))
        self.assertEqual(len(key), 87)  # 65-byte uncompressed point, base64url without padding

    def test_gone_subscription_removed(self):
        db = memdb()
        subs = [{"id": "a", "label": "iPhone", "sub": {"endpoint": "https://push/a"}},
                {"id": "b", "label": "Mac", "sub": {"endpoint": "https://push/b"}}]
        notify.put(db, "push_subs", subs)

        def fake(subscription_info, **_kw):
            if subscription_info["endpoint"].endswith("a"):
                raise Gone(410)
        with mock.patch.object(notify, "webpush", fake), mock.patch.object(notify, "WebPushException", Gone):
            self.assertEqual(notify.send_push(db, "Delta", "done", "/app"), 1)
        self.assertEqual([s["id"] for s in notify.get(db, "push_subs")], ["b"])

    def test_telegram_payload(self):
        db = memdb()
        notify.put(db, "telegram", {"token": "123:SECRET", "chat": "42"})
        seen = {}

        def fake(req, timeout=None):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return mock.MagicMock()
        with mock.patch.object(notify.urllib.request, "urlopen", fake):
            self.assertTrue(notify.send_telegram(db, "Delta: done"))
        self.assertEqual(seen["body"], {"chat_id": "42", "text": "Delta: done"})
        self.assertTrue(seen["url"].endswith("/bot123:SECRET/sendMessage"))

    def test_telegram_unset_is_false(self):
        self.assertFalse(notify.send_telegram(memdb(), "x"))

    def test_notify_get_hides_secrets(self):
        db = memdb()
        notify.put(db, "telegram", {"token": "123:SECRETTOKEN", "chat": "42"})
        notify.put(db, "push_subs", [{"id": "a", "label": "iPhone", "sub": {"endpoint": "https://push/zzz"}}])
        view = json.dumps(notify.public_view(db))
        self.assertNotIn("SECRETTOKEN", view)
        self.assertNotIn("endpoint", view)
        self.assertIn("OKEN", view)
        self.assertNotIn("private", view)


import tempfile  # noqa: E402


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        db.commit()
        self.states = {}
        self.sent = []
        self.n = notify.Notifier(self.path, lambda: list(self.states), lambda a: self.states[a],
                                 lambda a: a.capitalize(), base_url="https://bff")
        self.n.background = False
        self.patches = [mock.patch.object(notify, "send_push", lambda db, t, b, u, image=None: self.sent.append(("push", t, b, u)) or 1),
                        mock.patch.object(notify, "send_telegram", lambda db, text, photo=None: self.sent.append(("tg", text)) or True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_step_dispatches_once(self):
        notify.put(sqlite3.connect(self.path), "telegram", {"token": "1:x", "chat": "2"})
        self.states["delta"] = {"connected": True, "state": "RUNNING", "subtask_name": "cube"}
        self.n.step(0)
        self.states["delta"] = {"connected": True, "state": "FINISH", "subtask_name": "cube"}
        out = self.n.step(5) + self.n.step(10)
        self.assertEqual([k for _, k, _ in out], ["finished"])
        self.assertEqual(self.sent[0][:2], ("push", "Delta"))
        self.assertEqual(self.sent[0][3], "https://bff/app?printer=delta")
        self.assertTrue(self.sent[1][1].startswith("Delta: "))

    def test_disabled_channel_not_sent(self):
        notify.put(sqlite3.connect(self.path), "channels", {"telegram": False})
        self.states["delta"] = {"connected": True, "state": "RUNNING"}
        self.n.step(0)
        self.states["delta"] = {"connected": True, "state": "FINISH"}
        self.n.step(5)
        self.assertEqual([s[0] for s in self.sent], ["push"])

    def test_new_printer_mid_print(self):
        self.states["bravo"] = {"connected": True, "state": "FAILED", "end_reason": "failed"}
        self.assertEqual(self.n.step(0), [])

    def test_read_error_is_not_an_offline_alert(self):
        self.states["delta"] = {"connected": True, "state": "IDLE"}
        self.n.step(0)
        self.n.read_status = lambda a: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertEqual(self.n.step(10), [])
        self.assertEqual(self.n.step(131), [])  # Unreadable for longer than OFFLINE_S: still not offline.
        self.assertEqual(self.n.step(260), [])

    def test_real_disconnection_is_still_an_offline_alert(self):
        self.states["delta"] = {"connected": True, "state": "IDLE"}
        self.n.step(0)
        self.states["delta"] = {"connected": False, "state": "IDLE"}
        self.assertEqual(self.n.step(10), [])
        self.assertEqual([k for _, k, _ in self.n.step(131)], ["offline"])

    def test_failure_title_and_body_carry_the_reason(self):
        self.states["delta"] = {"connected": True, "state": "RUNNING", "subtask_name": "cube", "progress": 40}
        self.n.step(0)
        self.states["delta"] = {"connected": True, "state": "FAILED", "subtask_name": "cube", "progress": 40,
                               "attention": [{"handling": "sicurezza",
                                              "description": "Probe problem: Probe triggered prior to movement"}]}
        self.n.step(5)
        push = next(s for s in self.sent if s[0] == "push")
        self.assertEqual(push[1], "Delta: print failed")
        self.assertIn("Probe problem", push[2])
        self.assertIn("Probe triggered prior to movement", push[2])

    def test_idle_error_has_its_own_title(self):
        self.states["delta"] = {"connected": True, "state": "IDLE"}
        self.n.step(0)
        self.states["delta"] = {"connected": True, "state": "IDLE",
                               "attention": [{"handling": "sicurezza",
                                              "description": "HMS error 0300-0D00-0001-0001"}]}
        self.n.step(5)
        push = next(s for s in self.sent if s[0] == "push")
        self.assertEqual(push[1], "Delta: error")
        self.assertIn("0300-0D00-0001-0001", push[2])


APP = ROOT / "src" / "pygmy"


class AppSide(unittest.TestCase):
    def test_notify_set_keeps_token(self):
        db = memdb()
        notify.app_call(db, "notify_set", {"telegram": {"token": "1:SECRET", "chat": "5"}})
        notify.app_call(db, "notify_set", {"telegram": {"token": "", "chat": "6"}, "channels": {"telegram": False, "ha": False}})
        self.assertEqual(notify.get(db, "telegram"), {"token": "1:SECRET", "chat": "6"})
        self.assertEqual(notify.channels(db), {"push": True, "telegram": False})

    def test_subscribe_and_remove(self):
        db = memdb()
        sub = {"endpoint": "https://push/x", "keys": {"p256dh": "a", "auth": "b"}}
        dev = notify.app_call(db, "push_subscribe", {"sub": sub, "label": "iPhone"})["id"]
        again = notify.app_call(db, "push_subscribe", {"sub": sub, "label": "iPhone"})["id"]
        self.assertEqual(dev, again)  # same browser twice: one device
        self.assertEqual(notify.app_call(db, "notify_get", {})["devices"], [{"id": dev, "label": "iPhone"}])
        notify.app_call(db, "push_remove", {"id": dev})
        self.assertEqual(notify.get(db, "push_subs"), [])

    def test_subscribe_rejects_junk(self):
        with self.assertRaises(ValueError):
            notify.app_call(memdb(), "push_subscribe", {"sub": {"endpoint": "http://evil"}, "label": "x"})

    def test_notify_test_telegram_unset(self):
        self.assertFalse(notify.app_call(memdb(), "notify_test", {"channel": "telegram"})["ok"])

    def test_static_files(self):
        self.assertIn("showNotification", (APP / "sw.js").read_text())
        self.assertIn("notificationclick", (APP / "sw.js").read_text())
        man = json.loads((APP / "manifest.json").read_text())
        self.assertEqual((man["start_url"], man["display"]), ("/app", "standalone"))

    def test_options_has_notifications(self):
        html = (APP / "app.html").read_text()
        for text in ("Notifications", "Add to Home Screen", 'serviceWorker.register("/sw.js")', 'rel="manifest"',
                     "notify_get", "push_subscribe", "notify_test"):
            self.assertIn(text, html)
        src = (APP / "server.py").read_text()
        for name in ("notify_get", "notify_set", "push_subscribe", "push_remove", "notify_test", '"/sw.js"', '"/manifest.json"'):
            self.assertIn(name, src)


class ReviewFixes(unittest.TestCase):
    def two_subs(self, db):
        notify.put(db, "push_subs", [{"id": "a", "label": "Laptop", "sub": {"endpoint": "https://push/a"}},
                                     {"id": "b", "label": "iPhone", "sub": {"endpoint": "https://push/b"}}])

    def test_network_error_does_not_stop_others(self):
        db = memdb()
        self.two_subs(db)

        def fake(subscription_info, **_kw):
            if subscription_info["endpoint"].endswith("a"):
                raise ConnectionError("timeout")
        with mock.patch.object(notify, "webpush", fake):
            self.assertEqual(notify.send_push(db, "Delta", "done", "/app"), 1)
        self.assertEqual(len(notify.get(db, "push_subs")), 2)

    def test_prune_keeps_a_device_added_meanwhile(self):
        db = memdb()
        self.two_subs(db)

        def fake(subscription_info, **_kw):
            if subscription_info["endpoint"].endswith("a"):
                notify.app_call(db, "push_subscribe", {"sub": {"endpoint": "https://push/new", "keys": {"p256dh": "k", "auth": "a"}}, "label": "iPad"})
                raise Gone(410)
        with mock.patch.object(notify, "webpush", fake), mock.patch.object(notify, "WebPushException", Gone):
            notify.send_push(db, "Delta", "done", "/app")
        self.assertEqual([s["label"] for s in notify.get(db, "push_subs")], ["iPhone", "iPad"])

    def test_vapid_subject_not_localhost(self):
        db = memdb()
        self.two_subs(db)
        seen = []
        with mock.patch.object(notify, "webpush", lambda **kw: seen.append(kw["vapid_claims"]["sub"])):
            notify.send_push(db, "Delta", "done", "/app")
        self.assertTrue(seen and all("localhost" not in s and s.startswith(("mailto:", "https://")) for s in seen))

    def test_offline_at_boot_is_silent(self):
        p = Presence()
        self.assertEqual([p.update(False, 0), p.update(False, 300), p.update(True, 400)], [None, None, None])

    def test_disconnected_snapshot_no_alerts(self):
        n = notify.Notifier(":memory:", lambda: ["delta"], lambda a: state[0], lambda a: "Delta")
        n.background = False
        state = [{"connected": True, "state": "RUNNING", "ams_units": []}]
        n.step(0)
        state[0] = {"connected": False, "state": None}
        self.assertEqual(n.step(5), [])
        state[0] = {"connected": True, "state": "RUNNING"}
        self.assertEqual(n.step(10), [])

    def test_sw_tap_always_opens(self):
        sw = (APP / "sw.js").read_text()
        self.assertIn("clients.claim()", sw)
        self.assertIn(".catch(() => clients.openWindow(url))", sw)


class FirstRun(unittest.TestCase):
    def test_no_printer_picker_before_the_key(self):
        html = (APP / "app.html").read_text()
        self.assertIn("Welcome to Pygmy", html)
        self.assertIn("if (token) firstLoad();", html)
        self.assertIn("if (token && !PRINTERS.length) showPrinters();", html)
        self.assertNotIn("syncPrinters().finally(() => { if (!PRINTERS.length) showPrinters();", html)


class ShellCache(unittest.TestCase):
    """The installed app opens from a kept copy when the link is slow; the printers' data is never cached."""
    def test_only_the_page_and_its_icons_are_kept_network_first(self):
        sw = (APP / "sw.js").read_text()
        self.assertIn('const SHELL = ["/app", "/manifest.json", "/icon-192.png", "/icon-512.png"]', sw)
        self.assertIn("SHELL.includes(url.pathname)", sw)  # /mcp, /photo and the camera never pass through the cache
        self.assertIn("setTimeout(no, 4000)", sw)
