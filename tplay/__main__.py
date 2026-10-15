"""Run the terminal player: tplay"""

from __future__ import annotations

import shutil
import sys

USAGE = """\
tplay — terminal music player

Search, or paste a YouTube or Spotify playlist link.
Audio streams from YouTube and nothing is saved.
A Spotify playlist plays those songs from YouTube.

  tplay

Install that command once, from this folder:
  ./install

mpv has to be installed (the program that plays the audio):
  sudo apt install mpv

Inside the app: / search, enter play, + - volume, space pause, ? shortcuts.
"""


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(USAGE)
        return
    if args:
        print("tplay takes no arguments.\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    if shutil.which("mpv") is None:
        print("mpv is not installed. Install it first, for example:", file=sys.stderr)
        print("  sudo apt install mpv", file=sys.stderr)
        raise SystemExit(1)
    from tplay.app import TPlayApp

    TPlayApp().run()


if __name__ == "__main__":
    main()
