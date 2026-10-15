"""Version comparison and the GitHub lookup, without ever touching a network."""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from fun_tab import updates


def test_a_plain_bump_is_newer():
    assert updates.parse_version("0.5.0") > updates.parse_version("0.4.0")


def test_a_leading_v_does_not_matter():
    assert updates.parse_version("v0.4.0") == updates.parse_version("0.4.0")


def test_a_prerelease_suffix_is_ignored_rather_than_crashing():
    assert updates.parse_version("0.4.0-beta.1") == (0, 4, 0)


def test_garbage_parses_as_nothing_special():
    assert updates.parse_version("not-a-version") == (0,)
    assert updates.parse_version("") == (0,)


def test_is_newer_compares_the_whole_tuple():
    assert updates.is_newer("0.4.1", "0.4.0")
    assert updates.is_newer("0.5.0", "0.4.9")
    assert not updates.is_newer("0.4.0", "0.4.0")
    assert not updates.is_newer("0.3.9", "0.4.0")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_latest_release_reads_the_tag_and_page(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(
            {"tag_name": "v0.5.0", "html_url": "https://example.test/release"}
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    release = updates.fetch_latest_release()
    assert release == updates.Release(version="0.5.0", url="https://example.test/release")


def test_fetch_latest_release_carries_the_notes(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(
            {
                "tag_name": "v0.6.0",
                "html_url": "https://example.test/release",
                "body": "## Highlights\n- a thing\n",
            }
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    release = updates.fetch_latest_release()
    assert release.notes == "## Highlights\n- a thing"


def test_fetch_latest_release_notes_default_to_empty(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"tag_name": "v0.5.0"})

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    assert updates.fetch_latest_release().notes == ""


def test_fetch_latest_release_skips_a_prerelease(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"tag_name": "v0.5.0", "prerelease": True})

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    assert updates.fetch_latest_release() is None


@pytest.mark.parametrize(
    "fail",
    [
        lambda *_a, **_k: (_ for _ in ()).throw(URLError("offline")),
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError()),
        lambda *_a, **_k: _FakeResponse({"not": "json-shaped at all, but still parses"}),
    ],
)
def test_fetch_latest_release_never_raises(monkeypatch, fail):
    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    assert updates.fetch_latest_release() is None


def test_fetch_latest_release_tolerates_malformed_json(monkeypatch):
    class BadResponse(_FakeResponse):
        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: BadResponse({}))
    assert updates.fetch_latest_release() is None


def test_check_for_update_is_none_when_not_actually_newer(monkeypatch):
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        lambda repo=updates.REPO, timeout=updates._TIMEOUT: updates.Release(
            version="0.4.0", url="https://example.test"
        ),
    )
    assert updates.check_for_update("0.4.0") is None


def test_check_for_update_returns_the_release_when_newer(monkeypatch):
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        lambda repo=updates.REPO, timeout=updates._TIMEOUT: updates.Release(
            version="0.5.0", url="https://example.test"
        ),
    )
    release = updates.check_for_update("0.4.0")
    assert release is not None and release.version == "0.5.0"


def test_check_for_update_passes_none_through(monkeypatch):
    monkeypatch.setattr(
        updates, "fetch_latest_release", lambda repo=updates.REPO, timeout=updates._TIMEOUT: None
    )
    assert updates.check_for_update("0.4.0") is None


def test_fetch_latest_release_finds_the_installer_asset(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(
            {
                "tag_name": "v0.6.0",
                "html_url": "https://example.test/release",
                "assets": [
                    {"name": "FunTab.zip", "browser_download_url": "https://dl/zip", "size": 111},
                    {
                        "name": "FunTabSetup.exe",
                        "browser_download_url": "https://dl/setup.exe",
                        "size": 2048,
                    },
                ],
            }
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    release = updates.fetch_latest_release()
    assert release.installer_url == "https://dl/setup.exe"
    assert release.installer_size == 2048


def test_fetch_latest_release_without_an_installer_asset(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"tag_name": "v0.6.0", "assets": [{"name": "FunTab.zip"}]})

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    release = updates.fetch_latest_release()
    assert release.installer_url == ""
    assert release.installer_size == 0


class _FakeBinaryResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_asset_writes_the_file(monkeypatch, tmp_path):
    payload = b"MZ" + b"\x00" * 100
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda *_a, **_k: _FakeBinaryResponse(payload)
    )
    dest = tmp_path / "FunTabSetup.exe"
    assert updates.download_asset("https://dl/setup.exe", dest, expected_size=len(payload))
    assert dest.read_bytes() == payload


def test_download_asset_rejects_a_size_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda *_a, **_k: _FakeBinaryResponse(b"MZ" + b"\x00" * 10)
    )
    dest = tmp_path / "FunTabSetup.exe"
    assert not updates.download_asset("https://dl/setup.exe", dest, expected_size=99999)
    assert not dest.exists()


def test_download_asset_rejects_something_that_is_not_an_exe(monkeypatch, tmp_path):
    """A redirected-to HTML error page must not get a free pass as an installer."""
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeBinaryResponse(b"<html>404</html>"),
    )
    dest = tmp_path / "FunTabSetup.exe"
    assert not updates.download_asset("https://dl/setup.exe", dest)
    assert not dest.exists()


def test_download_asset_never_raises_when_offline(monkeypatch, tmp_path):
    from urllib.error import URLError

    def fail(*_a, **_k):
        raise URLError("offline")

    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    dest = tmp_path / "FunTabSetup.exe"
    assert not updates.download_asset("https://dl/setup.exe", dest)
