# Locked, hashed dependencies

Installers and CI install from these files with `pip install --require-hashes`,
so every build uses exactly the same packages, verified by hash, on every OS.

- `build.lock`: everything that goes into an installer (the app's runtime
  dependencies + PyInstaller), plus setuptools.
- `dev.lock`: runtime dependencies + test tools, plus setuptools.

Regenerate after changing a pin in `pyproject.toml` (universal: one file for
Windows, macOS and Linux):

```bash
pip install uv
uv pip compile pyproject.toml requirements/tools.in --extra build --universal \
    --python-version 3.12 --generate-hashes -o requirements/build.lock
uv pip compile pyproject.toml requirements/tools.in --extra dev --universal \
    --python-version 3.12 --generate-hashes -o requirements/dev.lock
```

Then install the app itself without dependencies:
`pip install --require-hashes -r requirements/build.lock && pip install --no-deps --no-build-isolation .`
