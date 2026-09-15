"""Fun Tab - a GTA-style radial Alt+Tab switcher for Windows."""

from __future__ import annotations

__version__ = "0.4.0"
__all__ = ["__version__", "main"]


def main() -> int:
    """Entry point; imported lazily so `fun_tab.__version__` stays cheap."""
    from .app import main as _main

    return _main()
