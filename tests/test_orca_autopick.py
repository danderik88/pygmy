import os
import sys
import unittest


class SignInWindowCloses(unittest.TestCase):
    def test_closes_once_signed_in_but_not_on_a_new_sign_in(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "slicer"))
        import slicerd as s
        proc = mock.Mock(poll=mock.Mock(return_value=None))
        state = {"in": False}
        with mock.patch.object(s, "_spawn", return_value=proc), mock.patch.object(s, "_end"), \
                mock.patch.object(s, "_orca_conf"), mock.patch.object(s, "orca_logged_in", lambda: state["in"]), \
                mock.patch.object(s, "_drive_login"):  # a real login thread would fail later and write "failed" into other tests' state
            s.start_login()
            s.gui_tick()
            self.assertIsNotNone(s._GUI["login"])
            state["in"] = True
            s.gui_tick()
            self.assertIsNone(s._GUI["login"])
            self.assertIsNotNone(s._GUI["proc"])  # Orca keeps running to sync
            s.start_login()
            s.gui_tick()
            self.assertIsNotNone(s._GUI["login"])
            s.stop_login()


if __name__ == "__main__":
    unittest.main()
