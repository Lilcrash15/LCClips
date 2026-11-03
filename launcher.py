import sys

# Fast path for `LCClips.exe --save-clip` (e.g. a Stream Deck "Open" action):
# poke the running LCClips and exit before loading the full app.
if "--save-clip" in sys.argv:
    from lcclips import winapp

    if winapp.signal_running("save"):
        sys.exit(0)

from lcclips.app import main

if __name__ == "__main__":
    main()
