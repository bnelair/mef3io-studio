"""PyInstaller entry script for the packaged app."""

import multiprocessing

if __name__ == "__main__":
    # FIRST, before importing the app: a helper process started by the
    # packaged app re-runs this executable; freeze_support() turns that run
    # into the helper and exits, so it never opens a window.
    multiprocessing.freeze_support()

    from mef3io_studio.app import main

    raise SystemExit(main())
