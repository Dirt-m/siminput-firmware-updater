"""The package version and pyproject must agree; __version__ went stale once."""
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import siminput_updater  # noqa: E402


class VersionInSync(unittest.TestCase):
    def test_dunder_version_matches_pyproject(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            declared = tomllib.load(f)["project"]["version"]
        self.assertEqual(siminput_updater.__version__, declared)


if __name__ == "__main__":
    unittest.main()
