"""Run a tour against the real window on made-up recordings and save one
screenshot per step (with the highlight ring) plus ``steps.json``.

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/manual/run_tour.py t1 out/dir

Used by build_manual.py; also handy while writing a tour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tourlib  # noqa: E402
from tourlib import Sink  # noqa: E402


class ShotSink(Sink):
    """One PNG per step: the screen after the step's actions, ring on the focus."""

    def __init__(self, out: Path):
        self.out = out
        self.records: list[dict] = []

    def begin(self, tour, ctx):
        self.out.mkdir(parents=True, exist_ok=True)
        self.tour = tour

    def shot(self, ctx, index, step, focus):
        image = tourlib.draw_highlight(ctx.grab(), focus)
        name = f"{self.tour.key}-{index + 1:02d}.png"
        image.save(str(self.out / name))
        self.records.append(
            {
                "image": name,
                "title": step.title,
                "caption": step.caption,
                "then": [a.label for a in step.acts("then") if a.label],
            }
        )

    def end(self, ctx):
        (self.out / f"{self.tour.key}.json").write_text(
            json.dumps(self.records, indent=1), encoding="utf-8"
        )


def run_to_folder(tour, out: Path, work: Path | None = None) -> list[dict]:
    work = work or tourlib.scratch()
    sink = ShotSink(out)
    try:
        tourlib.run_tour(tour, sink, work)
    finally:
        tourlib.cleanup(work)
    return sink.records


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    key, out = argv[0], Path(argv[1])
    tours = tourlib.load_tours([key])
    if not tours:
        print(f"No tour {key!r}.")
        return 2
    records = run_to_folder(tours[0], out)
    print(f"{tours[0].key}: {len(records)} steps saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
