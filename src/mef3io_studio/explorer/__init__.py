"""The Explore module: open recordings and see what is in them, without
loading their signal (docs/design/explorer.md, issue #4).

The widgets never call mef3io: every read runs as a background task through
the recording source (see ``workers.tasks``)."""

from .page import ExplorerPage

__all__ = ["ExplorerPage"]
