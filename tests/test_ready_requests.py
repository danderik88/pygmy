import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "pygmy" / "app.html").read_text()


class ReadyRequestsStayReachable(unittest.TestCase):
    """A sliced request waits for days: Print now lists it until it is printed or queued (the user 2026-11-25)."""

    def test_the_list_sits_above_the_steps(self):
        self.assertLess(PAGE.index('id="pnready"'), PAGE.index('id="step1"'))

    def test_only_ready_requests_with_a_file_are_listed_and_never_by_age(self):
        body = PAGE[PAGE.index("async function loadReady()"):PAGE.index("$(\"pnready\").onclick")]
        self.assertIn('r.status === "ready" && r.file_id != null', body)
        self.assertNotIn("expire", body)

    def test_opening_one_resumes_the_plate_list_from_the_plate_it_is_at(self):
        self.assertIn("(r.plates || []).slice(r.plate_index || 0)", PAGE)
        self.assertIn("await pnReady({ ...r, plates: remainingPlates(r) })", PAGE)

    def test_removing_one_takes_two_taps_and_closes_every_plate_left(self):
        self.assertIn('data-rm="${r.id}">Remove</button>', PAGE)
        self.assertIn('rm.textContent = "Sure?"', PAGE)
        self.assertIn("for (const _ of remainingPlates(r || {})) await rpc(\"print_requests\", { id: Number(rm.dataset.rm), status: \"started\" })", PAGE)

    def test_the_list_is_loaded_when_the_tab_opens(self):
        self.assertIn('if (b.dataset.t === "tab-new") { loadNew(); loadReady(); }', PAGE)

    def test_the_list_is_refreshed_with_the_queue(self):
        self.assertIn("async function loadQueue() {\n  loadReady();", PAGE)


if __name__ == "__main__":
    unittest.main()
