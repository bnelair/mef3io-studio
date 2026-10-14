"""``python -m mef3io_studio`` (accepts the same arguments as the installed app)."""

# Guarded: helper processes started with "spawn" re-import the main module
# under the name "__mp_main__", and must not start the app.
if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    from mef3io_studio.app import main

    raise SystemExit(main())
