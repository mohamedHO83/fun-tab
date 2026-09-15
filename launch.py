"""PyInstaller entry. ``python -m fun_tab`` still goes through ``__main__``."""

from fun_tab.app import main

if __name__ == "__main__":
    raise SystemExit(main())
