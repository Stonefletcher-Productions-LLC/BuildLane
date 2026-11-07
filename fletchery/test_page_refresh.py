"""An open window must be able to tell its screens are out of date and reload itself."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class PageIdTests(unittest.TestCase):
    def dash(self, ui: Path) -> server.Dashboard:
        dash = server.Dashboard.__new__(server.Dashboard)
        dash.token = "token-one"
        dash._page_key = None
        dash._page_hash = ""
        return dash

    def test_page_id_is_served_and_changes_with_screens_or_restart(self):
        with tempfile.TemporaryDirectory(prefix="fletchery-page-id-") as temp:
            ui = Path(temp)
            page = ui / "index.html"
            page.write_text('const TOKEN = "__FLETCHERY_TOKEN__"; const PAGE_ID = "__FLETCHERY_PAGE__";', encoding="utf-8")
            with patch.object(server, "UI", ui):
                dash = self.dash(ui)
                first = dash.page_id()
                served = dash.page().decode("utf-8")
                self.assertIn(first, served)
                self.assertNotIn("__FLETCHERY_PAGE__", served)
                self.assertEqual(dash.page_id(), first, "same screens and backend keep the same id")

                page.write_text("new screens, longer than before", encoding="utf-8")
                os.utime(page, ns=(1, 1))
                updated = dash.page_id()
                self.assertNotEqual(updated, first, "an installed UI change shows as a new id")

                dash.token = "token-two"  # what a backend restart looks like to the page
                self.assertNotEqual(dash.page_id(), updated)

    def test_missing_page_still_answers(self):
        with tempfile.TemporaryDirectory(prefix="fletchery-page-id-") as temp:
            with patch.object(server, "UI", Path(temp)):
                self.assertTrue(self.dash(Path(temp)).page_id())


if __name__ == "__main__":
    unittest.main()
