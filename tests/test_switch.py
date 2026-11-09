"""Firmware switching without hardware: UF2 and package checks, drive
lookup, the CmdMessenger parser, backup records and restore choice, and the
full flows driven against MockDevice + MockHardware (the same simulation the
--mock app runs, sped up)."""
import hashlib
import io
import json
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater import drives, mobiflight, releases, switch  # noqa: E402
from siminput_updater.config_history import Backup  # noqa: E402
from siminput_updater.firmware import FirmwareFile, FirmwarePackage  # noqa: E402
from siminput_updater.mobiflight import MobiFlightBox  # noqa: E402
from siminput_updater.mock_device import MOCK_CONFIG, MockDevice, MockHardware  # noqa: E402
from siminput_updater.operations import OperationCancelled  # noqa: E402


def uf2_block(family=switch.RP2040_FAMILY, flags=switch.UF2_FAMILY_FLAG):
    block = bytearray(512)
    struct.pack_into("<IIIIIIII", block, 0, 0x0A324655, 0x9E5D5157, flags, 0x10000000, 256, 0, 1, family)
    struct.pack_into("<I", block, 508, 0x0AB16F30)
    return bytes(block)


def mf_zip(uf2=None, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SIMINPUT/LICENSE.txt", "MIT")
        z.writestr("SIMINPUT/firmware/siminput_rev2_0_1_0.uf2", uf2 or uf2_block() * 4)
        z.writestr("SIMINPUT/firmware/reset.raspberry_pico_flash_nuke.uf2", uf2_block())
        z.writestr("SIMINPUT/boards/siminput_rev2.board.json", "{}")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def stock_package(version="2.7.0") -> FirmwarePackage:
    files = {
        "lib/serial_handler.py": f'FW_VERSION = "{version}"\n'.encode(),
        "lib/adafruit_bus_device/__init__.py": b"",
        "boot.py": b"# boot\n",
        "code.py": b"# code\n",
    }
    return FirmwarePackage(version, [FirmwareFile(p, hashlib.sha256(d).hexdigest(), len(d), d)
                                     for p, d in files.items()])


class FakeCtx:
    def __init__(self, cancel_on: str | None = None):
        self.cancel = threading.Event()
        self.statuses: list[str] = []
        self.logs: list[str] = []
        self._cancel_on = cancel_on

    @property
    def cancelled(self):
        return self.cancel.is_set()

    def check_cancel(self):
        if self.cancel.is_set():
            raise OperationCancelled()

    def status(self, message):
        self.statuses.append(message)
        if self._cancel_on and self._cancel_on in message:
            self.cancel.set()

    def log(self, message):
        self.logs.append(message)

    def progress(self, fraction):
        pass


class Uf2(unittest.TestCase):
    def test_bundled_files_are_intact_rp2040_uf2s(self):
        for name in (switch.NUKE_UF2, switch.CIRCUITPYTHON_UF2):
            switch.check_uf2(switch.asset(name), name)

    def test_rejects_junk_truncated_and_other_chips(self):
        with self.assertRaises(switch.SwitchError):
            switch.check_uf2(b"PK\x03\x04" * 128, "x")
        with self.assertRaises(switch.SwitchError):
            switch.check_uf2(uf2_block()[:300], "x")
        with self.assertRaises(switch.SwitchError):
            switch.check_uf2(uf2_block(family=0xE48BFF59), "x")   # RP2350
        switch.check_uf2(uf2_block(flags=0), "x")                 # no family id: fine

    def test_damaged_bundled_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / switch.NUKE_UF2).write_bytes(uf2_block())
            with mock.patch.object(switch, "ASSETS", Path(d)), self.assertRaises(switch.SwitchError):
                switch.asset(switch.NUKE_UF2)


class MobiFlightPackageLoading(unittest.TestCase):
    def test_release_zip_and_bare_uf2(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "siminput_rev2_0.1.0.zip"
            z.write_bytes(mf_zip())
            pkg = switch.load_mobiflight_package(z)
            self.assertEqual((pkg.version, pkg.uf2_name, pkg.has_community),
                             ("0.1.0", "siminput_rev2_0_1_0.uf2", True))
            u = Path(d) / "siminput_rev2_0_2_0.uf2"
            u.write_bytes(uf2_block())
            pkg = switch.load_mobiflight_package(u)
            self.assertEqual((pkg.version, pkg.has_community), ("0.2.0", False))

    def test_other_zips_and_bad_uf2s_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "x.zip"
            with zipfile.ZipFile(z, "w") as zz:
                zz.writestr("manifest.json", "{}")
            with self.assertRaises(switch.SwitchError):
                switch.load_mobiflight_package(z)
            z.write_bytes(mf_zip(uf2=b"not a uf2"))
            with self.assertRaises(switch.SwitchError):
                switch.load_mobiflight_package(z)

    def test_release_source(self):
        data = {"tag_name": "v0.1.0", "assets": [{
            "name": "siminput_rev2_0.1.0.zip", "size": 10,
            "browser_download_url": releases.MOBIFLIGHT.download_prefix + "v0.1.0/siminput_rev2_0.1.0.zip"}]}
        self.assertEqual(releases.parse_release(data, releases.MOBIFLIGHT).version, "0.1.0")
        with self.assertRaises(releases.ReleaseError):
            releases.parse_release(data)   # not a stock firmware asset


class CommunityInstall(unittest.TestCase):
    def test_unpacks_only_the_siminput_folder(self):
        with tempfile.TemporaryDirectory() as d:
            where = mobiflight.install_community(mf_zip(), Path(d))
            self.assertTrue((where / "boards" / "siminput_rev2.board.json").is_file())
            with self.assertRaises(mobiflight.MobiFlightError):
                mobiflight.install_community(mf_zip(extra={"SIMINPUT/../../evil.txt": "x"}), Path(d))
            with self.assertRaises(mobiflight.MobiFlightError):
                mobiflight.install_community(mf_zip(extra={"other/x.txt": "x"}), Path(d))
            self.assertFalse((Path(d).parent / "evil.txt").exists())


class CmdMessenger(unittest.TestCase):
    def test_split_and_parse_get_info_reply(self):
        msgs, rest = mobiflight.split_messages(b"10,SIMINPUT rev2,SIMINPUT rev2,SN-12/,3,0.1.0,2.0.0;7,B")
        self.assertEqual(rest, b"7,B")
        cmd, fields = mobiflight.parse_message(msgs[0])
        self.assertEqual(cmd, 10)
        self.assertEqual(fields, ["SIMINPUT rev2", "SIMINPUT rev2", "SN-12,3", "0.1.0", "2.0.0"])
        self.assertIsNone(mobiflight.parse_message("garbage"))

    def test_only_siminput_type_counts(self):
        self.assertTrue(MobiFlightBox("p", type="SIMINPUT rev2").is_siminput)
        self.assertFalse(MobiFlightBox("p", type="MobiFlight RaspiPico").is_siminput)
        self.assertFalse(MobiFlightBox("p", status="busy", type="SIMINPUT rev2").is_siminput)


class Drives(unittest.TestCase):
    def test_linux_mount_points_and_leftover_folders(self):
        with tempfile.TemporaryDirectory() as d:
            base, custom = Path(d) / "media", Path(d) / "mnt" / "CIRCUITPY"
            (base / "RPI-RP2").mkdir(parents=True)
            custom.mkdir(parents=True)
            mounts = Path(d) / "mounts"
            mounts.write_text(f"/dev/sdb1 {custom} vfat rw 0 0\n")
            self.assertEqual(drives._linux_drives("CIRCUITPY", mounts, [base]), [custom])
            self.assertFalse(drives.looks_mounted(base / "RPI-RP2", drives.BOOTSEL))  # empty leftover
            (base / "RPI-RP2" / "INFO_UF2.TXT").write_text("Board-ID: RP2350\n")
            self.assertFalse(drives.looks_mounted(base / "RPI-RP2", drives.BOOTSEL))
            (base / "RPI-RP2" / "INFO_UF2.TXT").write_text("Model: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n")
            self.assertTrue(drives.looks_mounted(base / "RPI-RP2", drives.BOOTSEL))

    def test_mac_volumes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "RPI-RP2 1").mkdir()
            self.assertEqual(drives._mac_drives("RPI-RP2", Path(d)), [Path(d) / "RPI-RP2 1"])

    def test_circuitpython_version(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "boot_out.txt").write_text(
                "Adafruit CircuitPython 9.2.8 on 2025-05-28; Raspberry Pi Pico with rp2040\n")
            self.assertEqual(switch.circuitpy_version(Path(d)), "9.2.8")
            (Path(d) / "boot_out.txt").write_text("Adafruit CircuitPython 9.2.8; Pico 2 with rp2350a\n")
            self.assertEqual(switch.circuitpy_version(Path(d)), "")


def backup(path: Path, name="Box", minutes=0) -> Backup:
    return Backup(path, datetime(2026, 11, 9, 12, minutes), name, 3, True)


class RestoreChoice(unittest.TestCase):
    def test_matches_the_box_then_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.json", Path(d) / "b.json"
            for p in (a, b):
                p.write_text(json.dumps(MOCK_CONFIG))
            backups = [backup(b, "Other", 30), backup(a, "Mine", 10)]   # newest first
            records = [{"id": "1", "backup": str(a), "ids": ["SN-A"], "state": "mobiflight"}]

            c = switch.choose_restore(["SN-A"], records, backups)
            self.assertEqual((c.backup.path, c.record_id, c.matched), (a, "1", True))
            # Unknown box: the newest backup, not claimed as matched.
            c = switch.choose_restore(["SN-X"], records, backups)
            self.assertEqual((c.backup.path, c.matched), (b, False))
            # Anonymous box (RPI-RP2): an unfinished switch, else blank.
            self.assertEqual(switch.choose_restore(None, records, backups).backup.path, a)
            records[0]["state"] = "restored"
            self.assertIsNone(switch.choose_restore(None, records, backups).backup)
            self.assertEqual(switch.choose_restore(None, [], []).config(), switch.DEFAULT_CONFIG)

    def test_records_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            rid = switch.add_record(Path(d) / "b.json", "Box", ["SN-A", ""], path)
            switch.update_record(rid, state="mobiflight", ids=["SN-A", "MF-1"], path=path)
            (r,) = switch.load_records(path)
            self.assertEqual((r["ids"], r["state"]), (["SN-A", "MF-1"], "mobiflight"))
            path.write_text("{broken")
            self.assertEqual(switch.load_records(path), [])


class Flows(unittest.TestCase):
    """The real flow code against the simulated box."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        backups = self.dir / "backups"

        def write_backup(config):
            backups.mkdir(exist_ok=True)
            p = backups / f"config-backup-{len(list(backups.iterdir()))}.json"
            p.write_text(json.dumps(config, indent=2))
            return p

        self.patches = [
            mock.patch.object(switch, "LOG_DIR", self.dir),
            mock.patch.object(switch, "_write_backup", write_backup),
            mock.patch.object(switch, "POLL_S", 0.005),
        ]
        for p in self.patches:
            p.start()
        self.backups = backups
        self.device = MockDevice(board_map="rev2")
        self.device.connect("MOCK")
        self.hw = MockHardware(self.device, time_scale=0.01)
        self.mf = switch.MobiFlightPackage("0.1.0", "siminput_rev2_0_1_0.uf2", uf2_block() * 4, mf_zip(),
                                           "siminput_rev2_0.1.0.zip")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_round_trip_restores_the_config(self):
        original = self.device.get_config()
        box = switch.to_mobiflight(self.device, self.hw, self.mf, FakeCtx(), self.hw.community_dir())
        self.assertEqual(self.hw._state, "mobiflight")
        self.assertTrue(box.is_siminput)
        self.assertFalse(self.device.connected)
        self.assertTrue((self.hw.community_dir() / "SIMINPUT" / "boards").is_dir())
        (rec,) = switch.load_records()
        self.assertEqual(json.loads(Path(rec["backup"]).read_text()), original)
        self.assertIn(box.serial, rec["ids"])

        scan = switch.Scanner(self.hw).scan()
        (found,) = scan.siminput_boxes
        choice = switch.choose_restore(found.ids, backups=[backup(Path(rec["backup"]))])
        self.assertTrue(choice.matched)

        self.device._config = {"device": {"name": "wiped"}, "rules": []}   # flashing wipes the box
        switch.to_siminput(self.hw, stock_package("2.7.1"), choice.config(), FakeCtx(), start="mobiflight",
                           box=found, record_id=choice.record_id, device=self.device)
        self.assertTrue(self.device.connected)
        self.assertEqual(self.device.get_config(), original)
        self.assertEqual(self.device.info.version, "2.7.1")
        self.assertEqual(switch.load_records()[0]["state"], "restored")

    def test_rev1_is_refused_before_anything_changes(self):
        self.device._board = "rev1"
        self.device.connect("MOCK")
        with self.assertRaises(switch.SwitchError):
            switch.to_mobiflight(self.device, self.hw, self.mf, FakeCtx())
        self.assertEqual(self.hw._state, "stock")
        self.assertTrue(self.device.connected)

    def test_missing_expander_is_refused(self):
        self.device.fault = "no_expander"
        with self.assertRaises(switch.SwitchError):
            switch.to_mobiflight(self.device, self.hw, self.mf, FakeCtx())
        self.assertEqual(self.hw._state, "stock")

    def test_failed_backup_stops_the_switch(self):
        def broken(_config):
            raise OSError("disk full")
        with mock.patch.object(switch, "_write_backup", broken), self.assertRaises(switch.SwitchError):
            switch.to_mobiflight(self.device, self.hw, self.mf, FakeCtx())
        self.assertEqual(self.hw._state, "stock")
        self.assertEqual(switch.load_records(), [])

    def test_cancel_after_the_bootloader_keeps_the_backup(self):
        ctx = FakeCtx(cancel_on="Installing")
        with self.assertRaises(OperationCancelled):
            switch.to_mobiflight(self.device, self.hw, self.mf, ctx)
        self.assertEqual(self.hw._state, "bootsel")
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.assertIn("RPI-RP2", ctx.logs[-1])
        # ...and the user can carry on from RPI-RP2.
        switch.install_mobiflight(self.hw, self.mf, FakeCtx())
        self.assertEqual(self.hw._state, "mobiflight")

    def test_fresh_box_from_bootsel_gets_the_default_config(self):
        self.hw._set("bootsel")
        switch.to_siminput(self.hw, stock_package(), switch.RestoreChoice(None).config(), FakeCtx(),
                           start="bootsel", device=self.device)
        self.assertEqual(self.device.get_config(), switch.DEFAULT_CONFIG)

    def test_stopped_after_circuitpython_can_be_finished(self):
        self.hw._set("bootsel")
        ctx = FakeCtx(cancel_on="Copying SIMINPUT")
        with self.assertRaises(OperationCancelled):
            switch.to_siminput(self.hw, stock_package(), MOCK_CONFIG, ctx, start="bootsel", device=self.device)
        self.assertEqual(self.hw._state, "circuitpy")
        scan = switch.Scanner(self.hw).scan()
        self.assertEqual(scan.circuitpy_version, "9.2.8")
        switch.to_siminput(self.hw, stock_package(), MOCK_CONFIG, FakeCtx(), start="circuitpy", device=self.device)
        self.assertEqual(self.device.get_config(), MOCK_CONFIG)

    def test_a_plain_pico_on_mobiflight_is_left_alone(self):
        self.hw._set("mobiflight")
        touched = []
        pico = MobiFlightBox(MockHardware.MF_PORT, type="MobiFlight RaspiPico")
        with mock.patch.object(self.hw, "probe_mobiflight", return_value=pico), \
                mock.patch.object(self.hw, "touch_1200", side_effect=touched.append):
            self.assertEqual(switch.Scanner(self.hw).scan().siminput_boxes, [])
            with self.assertRaises(switch.SwitchError):
                switch.to_siminput(self.hw, stock_package(), MOCK_CONFIG, FakeCtx(), start="mobiflight", box=pico)
        self.assertEqual(touched, [])

    def test_other_arduino_pico_boards_are_probed_once_per_plug_in(self):
        self.hw._set("mobiflight")
        other = MobiFlightBox(MockHardware.MF_PORT, status="no_response")
        with mock.patch.object(self.hw, "probe_mobiflight", return_value=other) as probe:
            scanner = switch.Scanner(self.hw)
            for _ in range(5):
                scanner.scan()
            self.assertEqual(probe.call_count, 1)
            other.status = "busy"   # busy ones are retried, to notice the Connector closing
            scanner.forget()
            for _ in range(3):
                scanner.scan()
            self.assertEqual(probe.call_count, 4)

    def test_connector_holding_the_port_is_reported(self):
        self.hw._set("mobiflight")
        held = MobiFlightBox(MockHardware.MF_PORT, status="busy", detail=mobiflight.CONNECTOR_HINT)
        with mock.patch.object(self.hw, "probe_mobiflight", return_value=held):
            self.assertEqual(len(switch.Scanner(self.hw).scan().busy_boxes), 1)
            with self.assertRaisesRegex(switch.SwitchError, "MobiFlight Connector"):
                switch.to_siminput(self.hw, stock_package(), MOCK_CONFIG, FakeCtx(), start="mobiflight",
                                   box=held)

    def test_drive_that_never_appears_times_out_clearly(self):
        self.hw._set("bootsel")
        with mock.patch.object(switch, "DRIVE_TIMEOUT", 0.05), \
                mock.patch.object(self.hw, "find_drive", side_effect=[self.hw.root / "RPI-RP2"] + [None] * 10**6):
            with self.assertRaisesRegex(switch.SwitchError, "RPI-RP2"):
                switch.to_siminput(self.hw, stock_package(), MOCK_CONFIG, FakeCtx(), start="bootsel")


class CachedDownload(unittest.TestCase):
    def test_reuses_a_matching_file(self):
        payload = b"zipbytes" * 10
        rel = releases.Release("0.1.0", "v0.1.0", "siminput_rev2_0.1.0.zip", "u", len(payload),
                               hashlib.sha256(payload).hexdigest(), "")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / rel.asset_name).write_bytes(payload)

            def no_network(*a, **k):
                raise AssertionError("downloaded again")
            self.assertEqual(releases.download(rel, Path(d), opener=no_network), Path(d) / rel.asset_name)


if __name__ == "__main__":
    unittest.main()
