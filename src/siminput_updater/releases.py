"""The latest firmware release on GitHub: look it up, download its package.

The download is checked before it is used: the URL must point into this
repo's release downloads, the file name must look like a firmware package,
the size must match what GitHub lists (and stay under MAX_BYTES), and the
SHA-256 must match the digest GitHub publishes for the asset. The zip then
goes through `firmware.load_firmware_zip` like a hand-picked file, which
checks every file against its manifest.

`opener` parameters exist so tests run without a network.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = "Dirt-m/siminput-firmware-v2"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
ASSET_NAME = re.compile(r"^siminput-firmware-[0-9A-Za-z.\-]+\.zip$")
MAX_BYTES = 10 * 1024 * 1024
CHUNK = 16 * 1024
TIMEOUT_S = 15


class ReleaseError(Exception):
    pass


@dataclass
class Release:
    version: str        # "2.7.0"
    tag: str            # "v2.7.0"
    asset_name: str
    url: str
    size: int
    sha256: str | None  # from GitHub's asset digest, when it publishes one
    page_url: str


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": "siminput-updater",
        "Accept": "application/vnd.github+json",
    })


def parse_release(data: dict) -> Release:
    """The release's firmware asset, or ReleaseError saying what is wrong."""
    if not isinstance(data, dict) or "tag_name" not in data:
        raise ReleaseError("GitHub sent something that isn't a release")
    tag = str(data["tag_name"])
    assets = [a for a in data.get("assets") or [] if ASSET_NAME.match(str(a.get("name", "")))]
    if not assets:
        raise ReleaseError(f"Release {tag} has no firmware package attached")
    a = assets[0]
    url = str(a.get("browser_download_url", ""))
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ReleaseError("The package link doesn't point to the firmware repository")
    size = a.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_BYTES:
        raise ReleaseError("The package has an unexpected size")
    digest = str(a.get("digest") or "")
    sha = digest[7:].lower() if digest.startswith("sha256:") else None
    return Release(version=tag.lstrip("vV"), tag=tag, asset_name=str(a["name"]), url=url,
                   size=size, sha256=sha, page_url=str(data.get("html_url", "")))


def fetch_latest(opener=urllib.request.urlopen) -> Release:
    try:
        with opener(_request(LATEST_API), timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read(1024 * 1024))
    except ReleaseError:
        raise
    except Exception as e:
        raise ReleaseError(f"Couldn't reach GitHub ({e})") from e
    return parse_release(data)


def download_dir() -> Path:
    return Path(tempfile.gettempdir()) / "siminput-firmware"


def download(release: Release, dest: Path | None = None,
             progress: Callable[[int, int], None] | None = None,
             should_cancel: Callable[[], bool] | None = None,
             opener=urllib.request.urlopen) -> Path:
    """Fetch the package to `dest` (a folder) and return its path. Written to
    a temporary name first, so a failed or cancelled download leaves nothing
    that looks like a package."""
    dest = dest or download_dir()
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / release.asset_name
    part = final.with_suffix(".part")
    h = hashlib.sha256()
    got = 0
    try:
        with opener(_request(release.url), timeout=TIMEOUT_S) as resp, open(part, "wb") as f:
            while True:
                if should_cancel and should_cancel():
                    raise ReleaseError("Download cancelled")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > release.size:
                    raise ReleaseError("The download is larger than GitHub said it would be")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(got, release.size)
        if got != release.size:
            raise ReleaseError(f"The download stopped early ({got} of {release.size} bytes)")
        if release.sha256 and h.hexdigest() != release.sha256:
            raise ReleaseError("The download doesn't match GitHub's checksum")
        part.replace(final)
        return final
    except ReleaseError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise ReleaseError(f"Download failed ({e})") from e
