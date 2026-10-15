"""Two short, optional sound cues: one on open, one on commit.

``winsound`` only exists on Windows, which is all this app targets anyway.
Playback is async and best-effort: a cue that ever delayed a keystroke or
raised would be worse than no sound at all, so every failure here is silent.
"""

from __future__ import annotations

from .paths import asset_path


def play(name: str) -> None:
    """Play ``assets/sounds/<name>.wav`` if it exists. Never raises."""
    try:
        import winsound

        path = asset_path(f"sounds/{name}.wav")
        if not path.exists():
            return
        # SND_NODEFAULT: a missing/corrupt file plays nothing rather than
        # substituting the generic Windows ding — the wrong sound is worse
        # than none. SND_ASYNC: never blocks the caller.
        flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        winsound.PlaySound(str(path), flags)
    except Exception:
        pass
