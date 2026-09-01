import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

import cloud_auth


class PermissionTests(unittest.TestCase):
    def test_atomic_update_preserves_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = cloud_auth.State(tmp)
            state.initialize()
            os.chmod(state.keys_path, 0o640)
            cloud_auth.add_key(state, "owner", None)
            self.assertEqual(state.keys_path.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
