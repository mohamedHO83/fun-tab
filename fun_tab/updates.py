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

REPO = "mohamedHO83/fun-tab"
_TIMEOUT = 4.0


@dataclass(frozen=True)
class Release:
    version: str  # e.g. "0.5.0" — no leading "v"
    url: str  # the release page to open


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
    return Release(version=tag.lstrip("vV"), url=page)


def check_for_update(
    current_version: str, *, repo: str = REPO, timeout: float = _TIMEOUT
) -> Release | None:
    """The latest release, but only if it is actually newer than what's running."""
    release = fetch_latest_release(repo, timeout)
    if release is None or not is_newer(release.version, current_version):
        return None
    return release
