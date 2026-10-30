"""The signal viewer (docs/design/viewer.md): a window that shows the signals
of one or more recordings.

The widgets never import mef3io. The recordings are opened by a background
task (``workers.tasks.open_viewer``) and read by worker threads through
``model.viewer_source`` and ``workers.viewer_scheduler``.
"""

from .window import ViewerWindow

__all__ = ["ViewerWindow"]
