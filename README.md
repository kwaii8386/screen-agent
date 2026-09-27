# screen-agent

**An MCP server that gives an AI eyes and hands on the local desktop.**

`screenshot` / `screen_record` / `mouse_click` / `type_text` / `key_press` / `mouse_drag` /
`mouse_scroll` — exposed over the Model Context Protocol, working on **Linux (Wayland and
X11), macOS and Windows** with no root and no permission dialogs.

---

## Why this exists

Taking a screenshot of a modern desktop from a script is harder than it looks:

| Approach | Result |
| --- | --- |
| `gnome-screenshot`, `org.gnome.Shell.Screenshot` | GNOME refuses untrusted D-Bus callers: `AccessDenied: Screenshot is not allowed` |
| XDG `Screenshot` portal | Shows a consent dialog, and hangs forever for a plain CLI caller |
| `ffmpeg -f x11grab`, `import -window root` | On Wayland these see only XWayland clients — typically 99.8% black pixels |
| `grim`, `sct` | wlroots-only; GNOME's Mutter does not implement those protocols |
| `mss`, `scrot` | X11 only |

The backend that actually works on GNOME/Wayland is **Mutter's own D-Bus API**:
`org.gnome.Mutter.ScreenCast` hands out a PipeWire stream (no prompt, no privilege), and
`org.gnome.Mutter.RemoteDesktop` injects real pointer and keyboard events. That is the
`mutter` backend here, and it was built after measuring each alternative on a live
GNOME 46 / Wayland session.

---

## Install

```bash
pipx install screen-agent            # or: uv tool install screen-agent
pipx install "screen-agent[all]"     # with every optional backend dependency
```

From a checkout:

```bash
pip install -e ".[all]"
```

Optional extras, installed only if you need them:

| Extra | Pulls in | Needed for |
| --- | --- | --- |
| `mutter` | `dbus-next` | GNOME/Wayland screen capture and input |
| `image` | `pillow` | downscaling, macOS/Windows capture |
| `mss` | `mss` | fast X11 capture |
| `all` | everything above | — |

`screen-agent` itself only hard-depends on `mcp`.

Check what works on *your* machine:

```bash
screen-agent doctor
```

```
screen-agent 0.1.0 on linux
  [OK  ] mutter           usable here
  [OK  ] wayland-tools    usable here
  [OK  ] x11              usable here
  [no  ] macos            not macOS
  [no  ] windows          not Windows

selected backend: mutter (GNOME / Mutter (D-Bus))
backend        : mutter (wayland+x11 on linux)
desktop        : 1920x1080 at (0, 0)
capabilities    : absolute_pointer=no, keyboard=yes, mouse=yes, record=yes, screenshot=yes
```

`doctor` tells you exactly which tool is missing and what to install, per backend.

---

## Connect it to an MCP client

`screen-agent serve` speaks MCP over stdio. Every MCP client launches servers the same way.

**opencode** — `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "screen-agent": {
      "type": "local",
      "command": ["screen-agent", "serve"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "screen-agent": { "command": "screen-agent", "args": ["serve"] }
  }
}
```

**Any other client** (Cursor, Windsurf, Continue, …) uses the same
`{"command": "screen-agent", "args": ["serve"]}` shape. With `uvx` you do not even have
to install it: `{"command": "uvx", "args": ["screen-agent", "serve"]}`.

---

## Tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `screen_info` | — | desktop size, active backend, per-capability support |
| `screenshot` | `path`, `region`, `cursor`, `scale` | PNG of the screen or a `x,y,w,h` region; returns the path |
| `screen_record` | `seconds`, `path` | records the screen to `.webm` |
| `mouse_move` | `x`, `y`, `cursor` | absolute pointer move, optional shape |
| `mouse_move_by` | `dx`, `dy` | relative pointer move |
| `mouse_click` | `x`, `y`, `button`, `clicks` | click, optionally moving first; `clicks=2` double |
| `mouse_drag` | `x1,y1,x2,y2`, `button`, `steps` | press, drag, release |
| `mouse_scroll` | `amount`, `direction`, `horizontal` | wheel notches |
| `type_text` | `text`, `interval_ms` | types literal text into the focused window |
| `key_press` | `keys` | `Return`, `Tab`, `ctrl+c`, `ctrl+shift+t`, `alt+F4`, … |

Coordinates are absolute pixels with `(0,0)` at the top-left of the whole desktop, as
reported by `screen_info`. Multi-monitor layouts with a negative origin work; the region
parser clamps to the desktop rectangle.

### The loop an agent should run

1. `screenshot()` → get a file path
2. **read that path** so the model actually sees the pixels
3. act — `mouse_click` / `type_text` / `key_press`
4. `screenshot()` again to verify

---

## CLI

Everything the MCP tools do is available directly, which makes installation easy to verify:

```bash
screen-agent info
screen-agent shot                          # -> ~/.cache/screen-agent/<stamp>.png
screen-agent shot out.png 0,0,800,600      # a region
screen-agent shot out.png full --scale 1280
screen-agent record 5 clip.webm
screen-agent move 960 540
screen-agent click 960 540
screen-agent click 960 540 right --clicks 2
screen-agent drag 300 400 900 400
screen-agent scroll 3 down
screen-agent type "hello world"
screen-agent key ctrl+shift+t
screen-agent doctor --json
```

---

## Backends

| Backend | Platform | Capture | Input | Needs |
| --- | --- | --- | --- | --- |
| `mutter` | GNOME on Wayland/X11 | Mutter ScreenCast → PipeWire → GStreamer | Mutter RemoteDesktop | `python3-dbus`, `gstreamer1.0-tools` |
| `wayland-tools` | any Wayland (Sway, Hyprland, river) | `grim` / `wlshot` / `wayshot` / `hyprshot` | `ydotool` / `dotool` / `wtype` / `xdotool` | those tools |
| `x11` | X11 sessions | `mss` → `import` → `ffmpeg` → `scrot`/`maim` | `xdotool` | `xdotool` + one capture tool |
| `macos` | macOS | `screencapture` → Pillow | CoreGraphics events | Screen Recording + Accessibility permission |
| `windows` | Windows 10/11 | Pillow `ImageGrab` | `user32` `SendInput` | Pillow |

Auto-detection walks that list in order and picks the first that fits. Force one with
`--backend NAME` or `SCREEN_AGENT_BACKEND=NAME`.

### Linux packages

```bash
# Debian / Ubuntu — GNOME or Wayland
sudo apt install python3-dbus gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-pipewire
sudo apt install xdotool grim ydotool        # X11 / wlroots extras
sudo apt install scrot imagemagick            # optional X11 capture fallbacks

# Fedora
sudo dnf install python3-dbus gstreamer1-tools pipewire-plugins
```

`ydotool` needs a daemon and `/dev/uinput` access:

```bash
echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo usermod -aG input "$USER"   # log out and back in
sudo modprobe uinput && ydotoold &
```

The `mutter` backend needs neither of those.

### Permissions on macOS

The first time you screenshot or type, grant your terminal (or the app hosting the MCP
client) **Screen Recording** and **Accessibility** in
*System Settings → Privacy & Security*. macOS otherwise silently returns a desktop picture
and drops synthetic events.

---

## Lessons from real-desktop testing

Driving a live GNOME 46 session end-to-end (open an app from the Activities overview,
type a 32-character search query, press Enter, read the results page) surfaced things a
unit test never would:

* **Activities swallows keystrokes.** Typing `firefox` into the GNOME overview produced
  `fic` — the shell grabs the first few characters while it is still building its search
  provider list, and the rest are dropped. Typing `brave` (5 characters) worked. When
  driving the overview, type in small chunks and screenshot to confirm, or launch apps
  from the dock instead.
* **There is no focus concept in the tool API.** `mouse_click(x, y)` is a coordinate, so
  nothing tells the agent which window receives it. After a click lands on a window that
  immediately closes (an overview, a transient popup), the *next* click is delivered to
  whatever is underneath. This produced several wrong-window clicks during testing. An
  agent must screenshot before every click and verify which window it is addressing.
* **Scrolling near screen edges triggers shell gestures.** `mouse_scroll` with the pointer
  near the bottom edge opened the Activities overview on GNOME instead of scrolling the
  page. Keep the pointer inside the content area, and prefer `key_press("pagedown")` when
  the target is a web page — it is more predictable than wheel events.
* **`super` is a usable standalone key.** `key_press("super")` toggles the overview and
  `key_press("super")` again closes it; `Escape` did *not* close the overview in testing.
* **Uppercase and shifted symbols need Shift held explicitly.** Mutter derives the keycode
  from the keysym, so a bare keysym for `A` or `!` does not produce the shifted character.
  The backend holds Shift for the characters that need it, which is why
  `type_text("Hello World 42!@#")` came out exactly right.

## Implementation notes



**Why absolute pointer motion is emulated on GNOME.**
Mutter 46 answers `NotifyPointerMotionAbsolute` with `Failed: No screen cast active` for
its native API, and the spec-compliant `CreateRemoteDesktop`/`SelectDevices` methods are
gone. So `mutter` homes the pointer with a `-100000,-100000` relative motion — the
compositor clamps it to the desktop origin, which makes the current position known
exactly — and then applies the precise delta. Geometry comes from Mutter's `Monitors`
property with an `xrandr` fallback, so multi-monitor layouts are handled.

**GStreamer invocation detail.** The pipeline must be passed as separate argv items with
bare `!` separators. Quoting the whole pipeline as one argument makes `gst-launch-1.0`
fail with `erroneous pipeline: syntax error`.

**Nothing blocks the event loop.** All process execution goes through
`asyncio.create_subprocess_exec`, D-Bus calls through `asyncio.to_thread`, and the MCP
tools are `async def`, so a long capture never stalls other clients.

**Screenshots on disk.** Paths are returned rather than image bytes so the model can
`read` the file and pick the tokens it needs. Files land in `~/.cache/screen-agent`
(or `%LOCALAPPDATA%\screen-agent` on Windows); override with `SCREEN_AGENT_DIR`.

---

## Development

```bash
git clone <this repo> && cd screen-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest                 # unit tests: region parsing, key parsing, backend detection
ruff check .
```

The test suite is pure-python and needs no display. End-to-end verification is manual:

```bash
screen-agent shot /tmp/check.png && python -c "from PIL import Image; print(Image.open('/tmp/check.png').size)"
```

---

## Status

| Backend | State |
| --- | --- |
| `mutter` (GNOME/Wayland) | verified end-to-end on Ubuntu 24.04 + GNOME 46: screenshots, typing, mouse move, CapsLock round-trip |
| `x11` | implemented, capture chain exercised in parts; needs a real X11 session to confirm |
| `wayland-tools` | implemented against documented `grim`/`ydotool` flags |
| `macos` | implemented against CoreGraphics; **untested — no macOS hardware here** |
| `windows` | implemented against `user32.SendInput`; **untested — no Windows host here** |

Issues on a platform I could not test are welcome; the backend interface is small, so a
fix is usually a few lines in one file.

## License

MIT — see [LICENSE](LICENSE).
