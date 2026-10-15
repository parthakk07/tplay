# tplay

Terminal music player. Search for a song, or paste a YouTube or Spotify playlist, and it plays in the terminal. Audio streams from YouTube. Nothing is saved to disk.

A Spotify playlist plays those songs from YouTube (up to 50). A YouTube playlist plays up to 100 songs.

## Install

You need Python 3.11 or newer, pipx, and mpv.

```bash
sudo apt install python3 pipx mpv
pipx ensurepath
```

Open a new terminal after `pipx ensurepath`, then:

```bash
git clone https://github.com/parthakk07/tplay.git
cd tplay
./install
```

`./install` puts `tplay` on your PATH. Leave the cloned folder where it is. The command runs from that folder.

From any terminal:

```bash
tplay
```

Quit with `ctrl+c`.

## Use

| Key | Action |
| --- | --- |
| `/` | focus search |
| enter | play the highlighted song |
| paste a link | queue a YouTube or Spotify playlist or video |
| `a` | add the highlighted song to the queue |
| space | play or pause |
| `n` / `p` | next / previous |
| `s` | shuffle |
| `r` | repeat the queue, then the current song |
| left / right | seek 10 seconds |
| shift+arrows | seek 30 seconds |
| `+` / `-` | volume |
| `m` | mute |
| `1` / `2` / `3` | search / queue / now playing |
| `j` / `k` | move through the list |
| `?` | shortcuts |
| ctrl+c | quit |

Click a row to play it. Click the progress bar to seek. Scroll or click the volume bar to change volume. Shuffle is the `⇄` button. Repeat is `↻`. Both turn green while they are on. Press repeat again and the button shows `1`, which replays the current song.

## Run without installing the command

From the project folder, `./play` creates a local virtualenv on the first run and opens the player. That does not add the `tplay` command.
