"""Privacy defaults: denylist, consent, and the privacy-mode preset.

These are the P0/P1 controls that keep Fun Tab from capturing or listing
sensitive apps unless the user deliberately opts back in.
"""

from __future__ import annotations

# Bump when the first-run wording changes enough that users should re-read it.
CONSENT_VERSION = 1

CONSENT_TEXT = (
    "Fun Tab replaces Alt+Tab on this PC.\n\n"
    "While it runs it will:\n"
    "• Watch keyboard and mouse shortcuts (it does not log what you type)\n"
    "• Optionally show window previews (off by default for minimised windows)\n"
    "• Start with Windows only if you turn that on later\n\n"
    "Click OK to continue, or Cancel to quit."
)

# Always excluded from the wheel and from PrintWindow, on top of exclude_exes.
PRIVACY_EXCLUDE_EXES: frozenset[str] = frozenset(
    {
        "1password.exe",
        "1password for windows desktop.exe",
        "bitwarden.exe",
        "keepass.exe",
        "keepassxc.exe",
        "keepass2.exe",
        "lastpass.exe",
        "dashlane.exe",
        "enpass.exe",
        "nordpass.exe",
        "roboform.exe",
        "sticky password.exe",
        "password safe.exe",
        "pwsafe.exe",
        "authenticator.exe",
        "authy desktop.exe",
        "credentialuibroker.exe",
        "consent.exe",
    }
)


def excluded_exes(user_exes: list[str] | tuple[str, ...] = ()) -> set[str]:
    """Built-in privacy denylist merged with the user's exclude_exes."""
    names = {name.lower() for name in PRIVACY_EXCLUDE_EXES}
    names.update(name.lower() for name in user_exes if name)
    return names


def apply_privacy_bundle(cfg) -> None:
    """Force the privacy-mode preset onto an already-constructed Config."""
    cfg.preview_enabled = False
    cfg.prefetch_previews = False
    cfg.capture_minimized = False
    cfg.close_key_enabled = False
    cfg.title_privacy = "app"
    cfg.dim_veil = 100
    if cfg.backdrop == "blur":
        cfg.backdrop = "dim"
