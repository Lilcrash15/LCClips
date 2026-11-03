"""LC Editor (lightweight video cutter), hosted inside LCClips.

Python port of the standalone Go LC Editor: same HTTP API and UI
(editor/ui/index.html), opened in a chromeless Edge app window, using
LCClips' bundled FFmpeg."""
from .server import Editor

__all__ = ["Editor"]
