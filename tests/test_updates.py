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
