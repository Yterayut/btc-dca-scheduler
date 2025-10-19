import importlib
import os
import unittest

from notifications.line_flex import build_basic_bubble, make_flex_message
import notify


class FlexEnvFlagTest(unittest.TestCase):
    def setUp(self):
        self._environ_backup = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._environ_backup)
        notify._refresh_flex_settings()

    def test_flex_disabled_by_default(self):
        os.environ.pop("LINE_USE_FLEX", None)
        os.environ.pop("LINE_FLEX_ALLOWLIST", None)
        notify._refresh_flex_settings()
        self.assertFalse(notify.flex_allowed("weekly_dca"))

    def test_allowlist_filters_channels(self):
        os.environ["LINE_USE_FLEX"] = "true"
        os.environ["LINE_FLEX_ALLOWLIST"] = "weekly_dca,s4_channel"
        notify._refresh_flex_settings()
        self.assertTrue(notify.flex_allowed("weekly_dca"))
        self.assertTrue(notify.flex_allowed("S4_Channel"))
        self.assertFalse(notify.flex_allowed("half_sell"))

    def test_allowlist_optional_when_empty(self):
        os.environ["LINE_USE_FLEX"] = "1"
        os.environ["LINE_FLEX_ALLOWLIST"] = ""
        notify._refresh_flex_settings()
        self.assertTrue(notify.flex_allowed("anything"))


class FlexBuilderTest(unittest.TestCase):
    def test_build_basic_bubble(self):
        bubble = build_basic_bubble(
            "Test Title",
            [("Label", "Value"), ("Another", "123")],
            subtitle="Subtitle",
            footer_note="Footer",
        )
        self.assertEqual(bubble["type"], "bubble")
        self.assertIn("body", bubble)
        body = bubble["body"]
        self.assertEqual(body["layout"], "vertical")
        self.assertGreaterEqual(len(body["contents"]), 4)

    def test_make_flex_message_wraps_bubble(self):
        bubble = build_basic_bubble("Title", [])
        message = make_flex_message("Alt text", bubble)
        self.assertEqual(message["type"], "flex")
        self.assertEqual(message["contents"], bubble)
        self.assertEqual(message["altText"], "Alt text")


if __name__ == "__main__":
    unittest.main()
