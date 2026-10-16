"""Run the terminal player: tplay"""

from __future__ import annotations

import sys

from tplay.player import mpv_install_hint

USAGE = f"""\
tplay - terminal music player

Search, or paste a YouTube or Spotify playlist link.
Audio streams from YouTube and nothing is saved.
A Spotify playlist plays those songs from YouTube.

  tplay

Install or update with one command.

  Linux and macOS:
    curl -fsSL https://raw.githubusercontent.com/parthakk07/tplay/main/install | bash

  Windows (PowerShell):
    irm https://raw.githubusercontent.com/parthakk07/tplay/main/install.ps1 | iex

If mpv is missing, tplay still opens and shows how to install it:
  {mpv_install_hint()}

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
    from tplay.app import TPlayApp

    TPlayApp().run()


if __name__ == "__main__":
    main()
