# User manual and tutorial videos

The manual (a PDF) and the tutorial videos are made **by the real app**, on
made-up recordings, from one source: the *tours* in `docs/manual/tours/`.
Rebuild both for every release, so the pictures and the videos always show
the current screens. Nothing here uses real data.

## Rebuild

From the repository root, with the development environment active
(`pip install -e ".[dev]"`):

```bash
export QT_QPA_PLATFORM=offscreen PYTHONPATH=src

# the PDF  ->  dist/manual/mef3io-studio-manual-<VERSION>.pdf
python scripts/manual/build_manual.py
#   --tour t1      only some tours      --preview DIR   also save page images
#   --shots DIR    keep/reuse the step screenshots

# the videos  ->  dist/manual/videos/<tour>-<name>.mp4
python scripts/manual/build_videos.py
#   --tour t1      only some tours      --fps 20   --format auto|mp4|apng

# one tour, screenshots only (handy while writing a tour)
python scripts/manual/run_tour.py t1 /tmp/shots
```

`dist/` is not committed. `tests/test_manual_tours.py` runs every tour
headless (no video), so a screen change that breaks a tour fails the tests.

### About the video format

`build_videos.py` writes **MP4 (H.264)** through Qt's multimedia
(`QMediaCaptureSession` + `QVideoFrameInput` + `QMediaRecorder`) when this
Qt can encode it. That needs the **full PySide6** (`pip install
PySide6-Addons==<the PySide6 version>`): the slim `PySide6-Essentials` has no
QtMultimedia. When it is missing, the script says so and writes an
**animated PNG** (`<tour>.png`; open it in a browser) with its own small
writer, which needs no extra program. The PDF needs only PySide6 itself.

## Write or change a tour

A tour is a file `docs/manual/tours/tN_name.py` defining `TOUR`. It is a list
of **steps**; each step has

- `do`: actions on the real window, each with a visible pointer target:
  `click("Next")` (the text a button, radio button, tab or list entry shows),
  `nav("Join or trim")`, `pick_rows([...])`, `type_in(field, "text")`,
  `drag_line("start", time)`, `do(function, target)`, `wait(condition, why)`;
- `caption`: plain words (the same words as the app, see
  `docs/ui-language.md`; a test checks the banned terms);
- `focus`: what gets the orange ring (default: what was acted on);
- `then`: what happens after the picture, usually `[click("Next")]`; the
  manual adds "Then choose Next." and the video clicks it;
- `hold`: seconds the video shows the scene.

`scripts/manual/tourlib.py` has the actions and the driver, and
`docs/manual/tours/_common.py` the steps that several tours share. The made-up
recordings (three nights, six channels, notes and events, a half-hour gap,
a made-up patient) are written by `tourlib.write_demo`.

When a tour breaks after a UI change, the error names what it looked for
("The tour looks for 'Next' on screen but it is not there"): update the tour,
or the text it points at.

## Adding to the tours later

Adding a step for a new option (for example "Notes and events: keep / leave
out" on the Privacy step) is a new `Step(...)` in the tour, with
`click("<its text>")`; nothing else changes. New tours are picked up
automatically (file names starting with `t`).
