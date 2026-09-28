# screen-agent

**An MCP server that gives an AI eyes and hands on the local desktop.**

`screenshot` / `screen_record` / `mouse_click` / `type_text` / `key_press` / `mouse_drag` /
`mouse_scroll` / `screen_state` / `page_click` — exposed over the Model Context Protocol,
working on **Linux (Wayland and X11), macOS and Windows** with no root and no permission
dialogs.

---

## Why this exists

### A screenshot is a last resort, not a first step

The obvious way to drive a desktop from a model is: screenshot, look at it, click a
coordinate, screenshot again. It works, and it is slow in a way that has nothing to do
with the screenshot. On a real task — search, read the results, jump to a later page, open
the third result — that loop took **six minutes across 15 tool calls**, and the capture
itself was 0.4% of it. What cost the time was 12 full-screen images fed to the model and
the reasoning each one demanded, plus measuring coordinates off a downscaled picture and
being off by 5–8%.

So the tools here are ordered by how much they actually tell you per unit of cost:

| | What it gives you | Measured |
| --- | --- | --- |
| `screen_state()` | window titles, rectangles, foreground app, pointer — **no pixels** | **0.25 ms** |
| `page_find(sel)` | exact link text, `href` and geometry | **~1 ms** |
| `page_click(sel, nth)` | the click, plus a verified before/after of the page | **~1 ms** |
| `screenshot(region=…)` | pixels | 17 ms (480×270) / 83 ms (full screen) |
| `screenshot()` + reading the image | pixels *and* tokens | 83 ms + 1–1.5k tokens |

Every figure is from `bench/bench_latency.py` on one machine, and re-derivable:
`python bench/bench_latency.py` prints the whole table and records the machine it ran
on. Nothing here is a claim about your hardware.

The same task, run against a local stand-in for a results page, with every click verified
by URL: **0.88 seconds and zero screenshots**, against roughly 360 seconds and 12 images
before.

### Capturing a modern desktop is harder than it looks

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

On the perception side the same measuring produced a clear ranking, and two things that
sound good and are not:

* **Windows UI Automation cannot see web content.** Measured on this machine: zero
  hyperlinks across three Chrome instances and Cốc Cốc, including with
  `--force-renderer-accessibility` and after enabling `chrome://accessibility`. It exposes
  the browser chrome — minimise, back, reload — and nothing below it, at 80–335 ms per
  walk. It is fine for native Win32 dialogs and useless for the web, which is most of
  what an agent is asked to drive.
* **The Chrome DevTools Protocol is exact and costs about a millisecond.** Given a
  browser started with `--remote-debugging-port`, "the third link on this page, and where
  to click it" is one `Runtime.evaluate`. No image, no coordinate estimation, no
  re-measuring.
* **Lowering the PNG compression level is a trap.** It makes the encode 2.3× faster and
  the file 38 KiB larger — and larger files mean more tokens, which is the actual
  bottleneck. It trades a rounding error in the wrong direction.

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
| `image` | `pillow` | faster `screenshot(scale=...)` and macOS capture fallbacks |
| `mss` | `mss` | fast X11 capture |
| `cdp` | `websockets` | the `page_*` tools: exact link text, hrefs and clicks in a Chromium browser |
| `all` | everything above | — |

`screen-agent` itself only hard-depends on `mcp`. On Windows it needs **nothing else**:
capture is GDI plus a PNG encoder built on `zlib`, and input is `user32.SendInput`
through `ctypes`. The `page_*` tools need the `cdp` extra and a browser you started
yourself; everything else works without them.

Check what works on *your* machine:

```bash
screen-agent doctor
```

```
screen-agent 0.2.0 on win32
  [no  ] mutter          DBUS_SESSION_BUS_ADDRESS is not set (no session bus)
  [no  ] wayland-tools   WAYLAND_DISPLAY is not set
  [no  ] x11             DISPLAY is not set
  [no  ] macos           not macOS
  [OK  ] windows         usable here

selected backend: windows (Windows (GDI capture + user32 SendInput))
backend        : windows (windows on win32)
desktop        : 1920x1080 at (0, 0)
capabilities    : absolute_pointer=yes, keyboard=yes, mouse=yes, record=yes, screenshot=yes
tools          : Pillow=yes, ffmpeg=C:\Program Files\ffmpeg\bin\ffmpeg.exe
note           : DPI awareness: per-monitor-v2 (coordinates are physical pixels)
note           : screen_record uses ffmpeg to write a video file
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

### Seeing

| Tool | Arguments | What it does |
| --- | --- | --- |
| `screen_info` | — | desktop size, active backend, per-capability support |
| `screen_state` | — | foreground window, visible windows with titles and rects, pointer, DPI mode. **No pixels, no image tokens** |
| `screenshot` | `path`, `region`, `cursor`, `scale`, `stable`, `stable_timeout_ms`, `max_age_ms` | PNG of the screen or a `x,y,w,h` region; returns the path |
| `screen_record` | `seconds`, `path` | records the screen to `.webm`/`.mp4` (or `.apng` without ffmpeg) |

### Acting

| Tool | Arguments | What it does |
| --- | --- | --- |
| `mouse_move` | `x`, `y`, `cursor` | absolute pointer move, optional shape |
| `mouse_move_by` | `dx`, `dy` | relative pointer move |
| `mouse_click` | `x`, `y`, `button`, `clicks` | click, optionally moving first; `clicks=2` double |
| `mouse_drag` | `x1,y1,x2,y2`, `button`, `steps` | press, drag, release |
| `mouse_scroll` | `amount`, `direction`, `horizontal` | wheel notches; `up`/`down` mean the same on every backend |
| `type_text` | `text`, `interval_ms` | types literal text into the focused window; `interval_ms=0` by default |
| `key_press` | `keys` | `Return`, `Tab`, `ctrl+c`, `ctrl+shift+t`, `alt+F4`, `+`, `space`, … |
| `screen_do` | `actions`, `stop_on_error` | runs a list of `{"op": …}` steps in one round trip and reports each |

### Reading a web page

Needs the `cdp` extra and a Chromium browser you started yourself.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `page_state` | — | the active tab's url, title, scroll position, pixel ratio. The cheap way to check whether a click worked |
| `page_find` | `selector`, `nth`, `limit`, `scroll_into_view` | exact text, `href` and geometry for every element a CSS selector matches |
| `page_click` | `selector`, `nth`, `scroll_into_view`, `verify`, `method` | clicks the nth match and returns the before/after difference |

These are named `page_*`, not `page_*`, on purpose. opencode ships its own
`tools.browser.*` namespace, and a run of the operator below showed why: an agent called
the *other* browser tool, saw it disconnected, concluded these were broken too, and
abandoned a working `page_find`. A name collision with the host's own tooling is a design
defect, not a cosmetic choice.

Coordinates are absolute pixels with `(0,0)` at the top-left of the whole desktop, as
reported by `screen_info`. Multi-monitor layouts with a negative origin work; the region
parser clamps to the desktop rectangle. `key_press` takes `+` and `space` as keys in
their own right (`"ctrl++"` is Ctrl and the plus sign), and every named key
`keys.py` knows about resolves on every backend - `tests/test_windows.py` and
`test_mutter_keysym_tables_cover_every_parsed_key` assert exactly that.

### Delegating a goal instead of driving it yourself

The loop below is 8 tool calls with no images — fine to do directly. It stops being fine
when a task needs twenty perception steps, or when the screenshots would pile up in the
main conversation. Then hand the whole thing to an operator subagent and get one JSON
object back.

Measured, same task both ways: 12 screenshots and 15 tool calls driven directly, versus
**0 screenshots and 8 tool calls** inside an operator, both ending on the same URL — and
the operator's `page_click` verified the navigation by URL rather than by looking at it.
Full write-up, the install block, the system prompt, a defensive result parser and the
honest limits are in [`docs/operator.md`](docs/operator.md).

**In a browser** — the `page_*` tools, and no image at all:

1. `page_state()` — which page am I on?
2. `page_find(selector)` — the exact links, with text and hrefs
3. `page_click(selector, nth)` — it returns whether the page actually moved
4. repeat

**For anything else:**

1. `screen_state()` — which window is in front? That is often all you needed to know
2. `screenshot(region=…)` → **read that path** so the model actually sees the pixels
3. act — `mouse_click` / `type_text` / `key_press`
4. `screenshot(stable=True, region=…)` again, cropped to the window that changed

### Two ways this goes wrong, and what the tools do about them

**Photographing a window that is still repainting.** A capture taken immediately after the
UI changes returns a stale frame — the pixels are from before the change, and every
coordinate read off it is wrong. `screenshot(stable=True)` re-captures until two
consecutive frames are identical, and tells you plainly when it gave up and the frame may
be mid-repaint. It never raises and never hangs.

**Clicking a coordinate that was measured a moment ago.** Between reading a rect and
clicking it, the window moves or the page reflows. `page_click` re-measures
immediately before dispatching, and scrolls the element into view first.

**A frame that is slightly old.** `max_age_ms` reuses a recent capture. It is **off by
default**, and every action clears the cache, so a frame is never served across a click.
When one is reused, the reply says how old it was.

### Driving a browser

The `page_*` tools attach to a Chromium browser (Chrome, Edge, Cốc Cốc) that you
started with a debug port and a profile of its own:

```bash
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/agent-browser
```

**A dedicated profile, on purpose.** `--user-data-dir` cannot be added to a browser that is
already running — Chrome ignores it and keeps the first instance's settings — so the
browser has to be started with the flag to begin with. A separate profile means the
automation gets no access to your cookies and no way to act as you in a logged-in
session, which is the right default for a tool that clicks things. Point the port at 9223
with `SCREEN_AGENT_CDP_PORT` if 9222 is taken.

If no browser is listening, these tools **raise an error naming both flags**. They never
fall back to guessing a coordinate: a click that lands on the wrong link while reporting
success is worse than an error, because the caller stops checking.

Two limits worth knowing, both found by measurement rather than by reading a spec:

* **An occluded window cannot receive synthetic clicks.** A browser window behind another
  window reports `document.visibilityState === "hidden"`, and Chrome accepts
  `Input.dispatchMouseEvent`, returns success, and delivers nothing.
  `Page.bringToFront` does not help, and neither does raising the window from the agent's
  process — Windows refuses `SetForegroundWindow` to a background process. So
  `page_click` reads `visibilityState` and, when the page is hidden, clicks inside the
  page instead. The result's `how` field says which was used: `input-event` fired real
  mouse events, `dom-click` fired none, so a menu that opens on `mousedown` will not
  respond. Pass `method` to force either.
* **Elements inside an iframe are not reachable** by a single
  `document.querySelectorAll`, and a virtualised list may not contain the row you asked
  for. Both raise. Neither is answered with a guess.

### Batching

`screen_do` takes a list of steps and runs them in order:

```json
[{"op": "type", "text": "hello"},
 {"op": "key", "keys": "Return"},
 {"op": "scroll", "amount": 5},
 {"op": "wait", "ms": 250}]
```

It is for steps you already know — typing a string, pressing a sequence of keys. Batching
trades away the chance to look between actions, so when you are not sure what is on screen,
take one screenshot and act with the single-action tools. An unknown `op` is rejected
before the first step runs, because a typo in step 30 should not be discovered after 29
real clicks.

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
| `windows` | Windows 10/11 | GDI `BitBlt` → PNG written with `zlib` | `user32.SendInput` | nothing (ffmpeg only for video) |

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

### Windows

Nothing to install: `screen-agent` is enough.

* **DPI awareness is not optional.** A process that is not DPI aware sees the 1536x864
  *logical* image of a 1920x1080 display at 125% scaling, while `SendInput` and the GDI
  screen DC are addressed in *physical* pixels, so every click lands ~20% off. The
  backend switches the process to per-monitor-v2 before touching the screen and
  `doctor` / `screen_info` report what it got.
* **The desktop is the virtual screen.** `SM_CXSCREEN` only covers the primary monitor;
  a second monitor to the left of it gives the desktop a negative origin, which is the
  coordinate space `GetCursorPos`, `SetCursorPos` and GDI all use.
* **Absolute pointer motion only.** Injected `MOUSEEVENTF_MOVE` deltas go through the
  pointer acceleration curve: an 18 pixel step arrives as 74, and a drag lands nowhere
  near where it was aimed. `mouse_move_by` and `mouse_drag` therefore convert to
  absolute coordinates (`MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`), which are
  pixel-exact.
* **Every gesture is one `SendInput` call.** Windows serialises the calls, not the
  events, so a button-down in one call and its up in another can be split by real
  hardware input - and a drag silently becomes a click.
* **`alt+F4` is a real `F4`.** Characters are resolved through `VkKeyScanW`, so
  `alt+F4` is `Alt`+`VK_F4` rather than a dead Alt with a Unicode `F`. Named punctuation
  (`lbracket`, `minus`, `plus`, …) is sent as the literal character instead, because the
  physical bracket key is a dead key on a Vietnamese layout and an accented letter on a
  French one.
* **Recording needs ffmpeg for a real video.** GDI frames are piped into ffmpeg's
  `rawvideo` demuxer, which gives a normal `.webm`/`.mp4`. Without ffmpeg the frames go
  into an APNG - the one animated image format that needs no encoder and plays in every
  browser - so `screen_record` always returns something watchable.
* **Two things are out of reach**, as on every automation tool for Windows: the UAC
  secure desktop, and windows elevated above the agent's own process (UIPI). Run the MCP
  client as the same user and at the same integrity level as the apps you drive.
* **A screenshot taken right after the UI changes is a stale frame.** The pixels are from
  before the change, so every coordinate read off it is wrong, and the mistake is
  invisible — the image looks fine. `screenshot(stable=True)` exists for this: it
  re-captures until two consecutive frames are identical.
* **Capture cost is not where the time goes.** A full-screen capture is ~83 ms, of which
  64% is PNG encoding — and virtually all of *that* is `zlib`, not the row loop — and
  40% is the GDI grab. Writing the file is 0.3%. `BitBlt` carries a ~16 ms fixed cost
  whatever the area, so a 480×270 region is 17 ms against 33 ms for the whole screen:
  **half**, not "about the same" as a first draft of this file claimed. Cropping pays,
  but it is not free.

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

Driving a real browser from Windows added three more, all of which cost a debugging
session to find:

* **An occluded browser window silently drops synthetic clicks.** See
  [Driving a browser](#driving-a-browser) — the short version is that `visibilityState`
  decides, and the tool reports which delivery method it used.
* **Reading the page right after a navigation returns an empty document.** The click
  succeeds, the next query finds nothing, and the obvious response is a sleep-and-retry
  loop that costs a round trip every time. `page_click` now waits for
  `document.readyState` itself, bounded, so the next read just works.
* **asyncio quantises every sleep to the Windows timer tick.** `asyncio.sleep(0.008)`
  really costs 15.6 ms, so the default 8 ms per-character typing gap made a 40-character
  string take 625 ms. The default is now no gap, and `type_text`'s parameter is still
  there for the rare caller who wants to watch it type. The gaps *between* gestures — the
  double-click interval, the per-step drag interval — are load-bearing and were left
  alone: they are what lets the target process each event.

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

**PNG encoding without a dependency.** `screen_agent.imaging` writes PNG and APNG files
with `zlib` and the standard library, decodes them back (all five scanline filters), and
downscales with an `itemgetter` gather. That is what lets the Windows backend take a
screenshot on a bare `pip install`, and it keeps `screenshot(scale=...)` working when
Pillow is absent.

Two honest limits in that decoder, both measured rather than assumed:

* **Writing a filter-0 PNG and reading one back is fast** — 12 ms for 1920×1080 — because
  filter 0 is a memcpy. Filters 1–4 are reconstructed byte by byte in Python, and there
  is no way around that without a C extension: 527 ms for a filter-1 image.
* **Pillow's own output is the bad case.** Measured on a real screenshot of this desktop,
  Pillow emits filter 2 for 65% of scanlines, filter 4 for 27% and filter 1 for 9% — so
  *every* line goes through the slow path, and 527 ms is the optimistic estimate. Decoding
  a Pillow-written 1920×1080 PNG this way takes 808 ms.

That is not a reason to drop the fallback: `scale_image` decodes the file it just wrote,
and `encode_png` writes filter 0, so `screenshot(scale=…)` without Pillow is ~34 ms and
fine. It is a reason to keep `pillow` in the `image` extra rather than treat the
pure-Python decoder as a fast path.

**`screenshot(scale=…)` shrinks inside GDI.** `scale` is handed to the backend, which
sizes its destination bitmap with `StretchBlt`/HALFTONE, so the full-size frame is never
encoded and never decoded: **33 ms against 117 ms** for 1920×1080 → 640, a 3.5×
difference, with the same output size.

---

## Development

```bash
git clone <this repo> && cd screen-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest                 # unit tests: regions, keys, images, backend detection, Win32 events
ruff check .
```

**Latency claims live in `bench/`, not in the test suite.** A test that waits 30 ms to
prove something is 3× faster is a test that fails on a busy machine and passes on broken
code, so the suite asserts the *call path* — that `screenshot(scale=N)` reaches the GDI
downscale rather than resizing afterwards — and every millisecond figure is measured here
instead:

```bash
python bench/bench_latency.py          # ~3 min: 32 measurements, markdown table + latest.json
python bench/bench_latency.py --quick  # 5 iterations, for a smoke check
```

`bench/latest.json` records the CPU, core count, Python and Pillow versions and the DPI
mode alongside the numbers, because a latency figure without its machine is not a fact.
`bench/README.md` has the method, the recorded baseline, and the noise evidence — a run
contended with another process came out ~20% slower across the board, which is why every
figure is reported as median and p95 with the sample size.

The unit tests need no display and do not touch the desktop. On Windows there is a second
layer, which *does* drive the real session - it moves the mouse, types into a throwaway
Win32 window and reads back what that window received:

```bash
python -m pytest tests/test_windows_e2e.py -q            # capture + record only
set SCREEN_AGENT_E2E=1 && python -m pytest tests/ -q      # plus live input injection
```

Screenshots and recording run by default on Windows; the input tests are opt-in because
they steal focus. Without `SCREEN_AGENT_E2E` you can still check the CLI by hand:

```bash
screen-agent shot /tmp/check.png && python -c "from PIL import Image; print(Image.open('/tmp/check.png').size)"
screen-agent record 3 /tmp/check.webm
```

---

## Status

| Backend | State |
| --- | --- |
| `mutter` (GNOME/Wayland) | verified end-to-end on Ubuntu 24.04 + GNOME 46: screenshots, typing, mouse move, CapsLock round-trip |
| `windows` | verified end-to-end on Windows 11 at 125% scaling: GDI screenshots, ffmpeg + APNG recording, and typing / click / double-click / drag-select / wheel against a live Win32 window |
| `x11` | implemented, capture chain exercised in parts; needs a real X11 session to confirm |
| `wayland-tools` | implemented against documented `grim`/`ydotool` flags |
| `macos` | implemented against CoreGraphics; **untested — no macOS hardware here** |

Issues on a platform I could not test are welcome; the backend interface is small, so a
fix is usually a few lines in one file.

## License

MIT — see [LICENSE](LICENSE).
