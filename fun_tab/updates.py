"""Check GitHub for a newer release — nothing else leaves the machine.

One request, made a few seconds after startup so it never competes with the
prewarm, cached for the rest of the process so it only happens once. Version
comparison is split from the network call so it can be tested without a
connection, and the network call itself never raises: a missed update check
must be invisible to the user, not an error dialog.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "mohamedHO83/fun-tab"
_TIMEOUT = 4.0
_DOWNLOAD_TIMEOUT = 60.0
INSTALLER_ASSET_NAME = "FunTabSetup.exe"


@dataclass(frozen=True)
class Release:
    version: str  # e.g. "0.5.0" — no leading "v"
    url: str  # the release page to open
    notes: str = ""  # the release body, Markdown as GitHub stored it
    installer_url: str = ""  # direct download for FunTabSetup.exe, if attached
    installer_size: int = 0  # bytes, from the API — a cheap integrity check


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.4.0"`` / ``"0.4.0-beta"`` -> ``(0, 4, 0)``.

    Stops at the first piece that is not a plain integer, so a pre-release
    suffix is ignored rather than making the version incomparable.
    """
    text = (text or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    nums: list[int] = []
    for part in re.split(r"[.+-]", text):
        if not part.isdigit():
            break
        nums.append(int(part))
    return tuple(nums) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest_release(repo: str = REPO, timeout: float = _TIMEOUT) -> Release | None:
    """The repo's latest published release. ``None`` on any failure at all —
    no internet, a timeout, a GitHub outage, a malformed response."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "fun-tab-update-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    page = str(data.get("html_url") or f"https://github.com/{repo}/releases")
    notes = str(data.get("body") or "").strip()
    installer_url = ""
    installer_size = 0
    for asset in data.get("assets") or ():
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "").lower() == INSTALLER_ASSET_NAME.lower():
            installer_url = str(asset.get("browser_download_url") or "")
            try:
                installer_size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                installer_size = 0
            break
    return Release(
        version=tag.lstrip("vV"),
        url=page,
        notes=notes,
        installer_url=installer_url,
        installer_size=installer_size,
    )


def download_asset(url: str, dest: Path, *, expected_size: int = 0, timeout: float = _DOWNLOAD_TIMEOUT) -> bool:
    """Download one release asset to ``dest``. ``False`` on any failure —
    no internet, a timeout, a truncated transfer, a file that doesn't even
    look like a Windows executable. Never raises.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "fun-tab-update-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except Exception:
        return False
    if expected_size and len(data) != expected_size:
        return False
    if data[:2] != b"MZ":  # the DOS header every PE (.exe) starts with
        return False
    try:
        dest.write_bytes(data)
    except OSError:
        return False
    return True


def check_for_update(
    current_version: str, *, repo: str = REPO, timeout: float = _TIMEOUT
) -> Release | None:
    """The latest release, but only if it is actually newer than what's running."""
    release = fetch_latest_release(repo, timeout)
    if release is None or not is_newer(release.version, current_version):
        return None
    return release
