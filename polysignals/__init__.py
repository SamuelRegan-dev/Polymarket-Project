"""Signal research on committee-decided Polymarket markets."""

import sys

# The logs and analysis output use a few non-ASCII glyphs (arrows, box
# rules). On a Windows console defaulting to cp1252 those raise
# UnicodeEncodeError, so force UTF-8 on the standard streams if the
# runtime allows it. Harmless everywhere else.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
