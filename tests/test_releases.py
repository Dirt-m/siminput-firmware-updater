import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.releases import (  # noqa: E402
    DOWNLOAD_PREFIX, Release, ReleaseError, download, fetch_latest, parse_release,
)

PAYLOAD = b"PK fake zip bytes" * 100


def _release_json(**over):
    asset = {"name": "siminput-firmware-2.7.0.zip", "size": len(PAYLOAD),
             "digest": "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
             "browser_download_url": DOWNLOAD_PREFIX + "v2.7.0/siminput-firmware-2.7.0.zip"}
    asset.update(over)
    return {"tag_name": "v2.7.0", "html_url": "https://github.com/x", "assets": [
        {"name": "notes.txt", "size": 3, "browser_download_url": DOWNLOAD_PREFIX + "v2.7.0/notes.txt"}, asset]}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(body: bytes):
    return lambda req, timeout=None: _Resp(body)


class Parse(unittest.TestCase):
    def test_picks_the_package(self):
        r = parse_release(_release_json())
        self.assertEqual((r.version, r.asset_name, r.size), ("2.7.0", "siminput-firmware-2.7.0.zip", len(PAYLOAD)))
        self.assertEqual(r.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_rejects_foreign_links_and_odd_sizes(self):
        with self.assertRaises(ReleaseError):
            parse_release(_release_json(browser_download_url="https://evil.example/siminput-firmware-2.7.0.zip"))
        with self.assertRaises(ReleaseError):
            parse_release(_release_json(size=0))
        with self.assertRaises(ReleaseError):
            parse_release({"tag_name": "v1", "assets": []})
        with self.assertRaises(ReleaseError):
            parse_release({"message": "Not Found"})

    def test_fetch_latest_parses_api_json(self):
        r = fetch_latest(opener=_opener(json.dumps(_release_json()).encode()))
        self.assertEqual(r.tag, "v2.7.0")

    def test_fetch_latest_wraps_network_errors(self):
        def broken(req, timeout=None):
            raise OSError("no route")
        with self.assertRaises(ReleaseError):
            fetch_latest(opener=broken)


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rel = parse_release(_release_json())

    def test_good_download(self):
        seen = []
        path = download(self.rel, self.dir, progress=lambda a, b: seen.append(a), opener=_opener(PAYLOAD))
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(seen[-1], len(PAYLOAD))

    def test_checksum_mismatch_leaves_nothing(self):
        bad = Release(**{**self.rel.__dict__, "sha256": "0" * 64})
        with self.assertRaises(ReleaseError):
            download(bad, self.dir, opener=_opener(PAYLOAD))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_short_and_long_downloads_fail(self):
        with self.assertRaises(ReleaseError):
            download(self.rel, self.dir, opener=_opener(PAYLOAD[:-10]))
        with self.assertRaises(ReleaseError):
            download(self.rel, self.dir, opener=_opener(PAYLOAD + b"extra"))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_cancel(self):
        with self.assertRaises(ReleaseError):
            download(self.rel, self.dir, should_cancel=lambda: True, opener=_opener(PAYLOAD))


if __name__ == "__main__":
    unittest.main()
