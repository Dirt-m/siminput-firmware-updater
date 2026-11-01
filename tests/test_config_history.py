import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.config_history import list_backups, read_backup  # noqa: E402

CONFIG = {"device": {"name": "Wheel Box", "pid": 61440}, "bools": [], "axes": [],
          "rules": [{"type": "MAP", "input": "D1", "output": "B1"},
                    {"type": "MAP", "input": "D2", "output": "B2"},
                    {"note": "a comment"}]}


class History(unittest.TestCase):
    def setUp(self):
        self.a, self.b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (self.a / "config-backup-20261101-120000.json").write_text(json.dumps(CONFIG))
        (self.b / "config-backup-20261102-090000.json").write_text(json.dumps(CONFIG))
        (self.a / "config-backup-20261030-080000.json").write_text("{not json")
        (self.a / "updater.log").write_text("log")

    def test_newest_first_across_folders(self):
        backups = list_backups([self.a, self.b])
        self.assertEqual([b.path.name for b in backups], [
            "config-backup-20261102-090000.json", "config-backup-20261101-120000.json",
            "config-backup-20261030-080000.json"])
        self.assertEqual((backups[0].name, backups[0].rules, backups[0].readable), ("Wheel Box", 2, True))
        self.assertEqual(backups[0].when.hour, 9)

    def test_unreadable_is_listed_not_fatal(self):
        bad = list_backups([self.a])[-1]
        self.assertFalse(bad.readable)

    def test_same_folder_twice_is_one_list(self):
        self.assertEqual(len(list_backups([self.a, self.a])), 2)

    def test_missing_folder(self):
        self.assertEqual(list_backups([self.a / "nope"]), [])

    def test_read_backup_round_trips(self):
        config = read_backup(self.a / "config-backup-20261101-120000.json")
        self.assertEqual(config.device.name, "Wheel Box")


if __name__ == "__main__":
    unittest.main()
