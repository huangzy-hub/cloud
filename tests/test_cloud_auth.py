import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

import cloud_auth


class CloudAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = cloud_auth.State(self.tmp.name)
        self.state.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_verify_and_session(self):
        key = cloud_auth.add_key(self.state, "owner", None)
        entry = cloud_auth.verify_access_key(self.state, key)
        self.assertIsNotNone(entry)
        token = cloud_auth.issue_session(self.state, entry)
        self.assertEqual(cloud_auth.verify_session(self.state, token)["name"], "owner")

    def test_wrong_key_and_revoke(self):
        key = cloud_auth.add_key(self.state, "owner", None)
        self.assertIsNone(cloud_auth.verify_access_key(self.state, key + "x"))
        entry = cloud_auth.verify_access_key(self.state, key)
        token = cloud_auth.issue_session(self.state, entry)
        cloud_auth.set_enabled(self.state, "owner", False)
        self.assertIsNone(cloud_auth.verify_access_key(self.state, key))
        self.assertIsNone(cloud_auth.verify_session(self.state, token))

    def test_rotate_invalidates_old_key_and_session(self):
        old_key = cloud_auth.add_key(self.state, "owner", None)
        entry = cloud_auth.verify_access_key(self.state, old_key)
        old_session = cloud_auth.issue_session(self.state, entry)
        new_key = cloud_auth.rotate_key(self.state, "owner", None)
        self.assertIsNone(cloud_auth.verify_access_key(self.state, old_key))
        self.assertIsNotNone(cloud_auth.verify_access_key(self.state, new_key))
        self.assertIsNone(cloud_auth.verify_session(self.state, old_session))

    def test_expired_key(self):
        key = cloud_auth.add_key(self.state, "temp", int(time.time()) - 1)
        self.assertIsNone(cloud_auth.verify_access_key(self.state, key))

    def test_safe_next(self):
        self.assertEqual(cloud_auth.safe_next("/SSD/a?b=1"), "/SSD/a?b=1")
        self.assertEqual(cloud_auth.safe_next("//evil.example"), "/")
        self.assertEqual(cloud_auth.safe_next("https://evil.example"), "/")

    def test_login_page_renders_css_and_escapes_values(self):
        page = cloud_auth.build_login_page('/SSD/\"<test>', "bad <key>").decode("utf-8")
        self.assertIn(":root{color-scheme:dark}", page)
        self.assertIn("bad &lt;key&gt;", page)
        self.assertIn('value="/SSD/&quot;&lt;test&gt;"', page)
        self.assertNotIn("{error}", page)
        self.assertNotIn("{next}", page)

    def test_origin_check_normalizes_proxy_host_ports(self):
        self.assertTrue(
            cloud_auth.origin_allowed(
                "https://cloud.example.com",
                "127.0.0.1:18081",
                "cloud.example.com:443",
            )
        )
        self.assertFalse(
            cloud_auth.origin_allowed(
                "https://evil.example",
                "cloud.example.com",
                "cloud.example.com",
            )
        )
        self.assertTrue(
            cloud_auth.origin_allowed(
                "null",
                "cloud.example.com",
                "cloud.example.com",
            )
        )
        self.assertFalse(
            cloud_auth.origin_allowed(
                "null",
                "127.0.0.1:18081",
                "evil.example",
            )
        )


if __name__ == "__main__":
    unittest.main()
