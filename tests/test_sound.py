"""Open/commit sound cues — off by default, never allowed to raise."""

from __future__ import annotations

from fun_tab import sound


def test_the_real_sound_files_exist_and_are_valid_wav():
    """winsound only reports failure as silence, so this is the one place a
    missing or corrupt asset would actually be caught."""
    import wave

    for name in ("open", "commit", "cycle"):
        path = sound.asset_path(f"sounds/{name}.wav")
        assert path.exists(), f"{path} is missing"
        with wave.open(str(path), "rb") as f:
            assert f.getnframes() > 0

def test_play_calls_winsound_with_the_right_file(monkeypatch, tmp_path):
    calls = []

    class FakeWinsound:
        SND_FILENAME = 1
        SND_ASYNC = 2
        SND_NODEFAULT = 4

        @staticmethod
        def PlaySound(path, flags):
            calls.append((path, flags))

    wav = tmp_path / "sounds" / "open.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF....WAVEfmt ")

    monkeypatch.setattr(sound, "asset_path", lambda name: tmp_path / name)
    monkeypatch.setitem(__import__("sys").modules, "winsound", FakeWinsound)

    sound.play("open")

    assert len(calls) == 1
    path, flags = calls[0]
    assert path == str(wav)
    assert flags == FakeWinsound.SND_FILENAME | FakeWinsound.SND_ASYNC | FakeWinsound.SND_NODEFAULT


def test_play_is_a_no_op_when_the_file_is_missing(monkeypatch, tmp_path):
    calls = []

    class FakeWinsound:
        SND_FILENAME = 1
        SND_ASYNC = 2
        SND_NODEFAULT = 4
        PlaySound = staticmethod(lambda *a: calls.append(a))

    monkeypatch.setattr(sound, "asset_path", lambda name: tmp_path / "nope.wav")
    monkeypatch.setitem(__import__("sys").modules, "winsound", FakeWinsound)

    sound.play("open")

    assert calls == []


def test_play_never_raises_even_if_winsound_blows_up(monkeypatch, tmp_path):
    class FakeWinsound:
        SND_FILENAME = 1
        SND_ASYNC = 2
        SND_NODEFAULT = 4

        @staticmethod
        def PlaySound(*_a):
            raise OSError("no audio device")

    wav = tmp_path / "open.wav"
    wav.write_bytes(b"x")
    monkeypatch.setattr(sound, "asset_path", lambda name: wav)
    monkeypatch.setitem(__import__("sys").modules, "winsound", FakeWinsound)

    sound.play("open")  # must not raise
