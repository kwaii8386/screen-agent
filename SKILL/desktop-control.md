# Driving the desktop with `screen-agent`

Everything below was measured on the target machine. The failures are the valuable half:
each one cost a run to find, and each one is a rule you can follow to skip it.

---

## 1. The cost model — pick the perception that fits the question

| Path | Cost | Tokens | Exact? | Use when |
| --- | --- | --- | --- | --- |
| `screen_state` | **0.25 ms** | 0 | yes | "which window is in front", "is anything on screen" |
| `page_state` / `page_find` / `page_click` | **~1 ms** | 0 | yes | anything inside a Chromium browser |
| `ui_find` / `ui_click` | **46–170 ms** | 0 | yes | any app that exposes its controls |
| `screenshot(region)` | 17 ms | 0 | no | you need pixels: canvas, image, custom-drawn UI |
| `screenshot()` + read the file | 83 ms | **1000–1500** | no | last resort |

**A screenshot is the most expensive way to learn something that is usually already known
exactly.** Before you take one, ask: is this data, or is it pixels? Most of the time it is
data, and there is a tool that returns it as data.

The corollary that matters: if you take a screenshot *and* then find the answer was in
`ui_find` or `page_find`, that was a mistake. Say so — it is the cheapest way to find out
which tool you should have reached for.

---

## 2. Decision rules, by situation

### 2.1 Chromium browser (Chrome, Edge, CocCoc, Electron)

Use `page_*`. Never `ui_find` for page content — measured **0 hyperlinks** across four
browser instances, even with `--force-renderer-accessibility`.

```
page_state()                      # which tab, which url
page_find("a[href*='first=']")    # exact text, href, rect
page_click(sel, nth=2)            # returns whether the page actually moved
```

Needs the browser started with `--remote-debugging-port=9222` and its **own**
`--user-data-dir`. If it is not listening these tools **raise with the flags named** and
never fall back to guessing a coordinate. Honour that: report it, do not work around it.

`page_click` returns `how`:
- `input-event` — real mouse events were delivered; page scripts saw them
- `dom-click` — the page was occluded and was clicked from inside it; **no mouse events
  fired**, so a menu that opens on `mousedown` will not open

### 2.2 A native app that exposes its controls

Use `ui_find` / `ui_click` against the window's `hwnd` from `screen_state`.

```
screen_state()                    # find the hwnd and the window class
window_activate(hwnd)             # bring it forward — CHECK is_foreground, see 3.2
ui_find(hwnd, kinds=["Button"])   # exact names, kinds, rectangles
ui_click("Save", hwnd)            # re-reads the rect first, warns about occluders
```

`ui_find` costs about **1.2 ms per element** — one cross-process COM round trip per
property. It reads three properties per node, and that restraint is the reason it is usable.
**One walk beats a filtered query**: `FindAll` with a control-type condition measured
**221 ms against 46 ms**, because each condition makes the provider traverse the whole
tree again. Do not "optimise" by querying per control type.

Needs the `windows` extra. Cannot see web content, and cannot see minimised windows
(their rectangles are all zero).

### 2.3 A custom-drawn UI — ribbon apps, game engines, Electron chrome

**You cannot address these by name.** WPS Writer exposes 62 elements, 5 of them named, and
none of them are the font or size box. Neither will the DOM. Your only perception is a
screenshot.

For these, the rules in §3 are not optional — they are the difference between working and
silently doing nothing.

### 2.4 Opening an app

**Always open it maximised, unless the task needs two windows side by side.**

A maximised window is one predictable rectangle. A restored window can be anywhere, and
every coordinate you measured goes stale when the user or the app moves it. If you do not
need the size, take it.

```
key_press("win+r")                       # 40 ms
type_text(r"D:\App\app.exe")             # 16 ms
key_press("Return")                      # 40 ms
```

**Batch these. Do not perceive between them** — see §5.

From the Start menu: **do not use it.** It lives about a second, and the round trip
through a model takes longer than that. You will arrive after it is gone, having measured
coordinates against a menu that no longer exists. `Win+R` is faster (106 ms for all three
calls) and deterministic.

### 2.5 Two windows side by side

Only when the task genuinely needs both visible. Otherwise one maximised window is better
than two half-width ones.

The hazard is **occlusion**: a click at exact coordinates goes to whatever is on top, so a
control in the background window receives nothing while the click tool reports success.
`ui_click` warns about this, but only if it can see the blocker — so:

```
screen_state()                   # z-order is front to back
window_activate(hwnd_a)          # put A in front
ui_click(..., hwnd=a)            # act on A
window_activate(hwnd_b)          # put B in front
ui_click(..., hwnd=b)            # act on B
```

Alternate the activation before every action that targets a background window. Do not
activate once and then act on both.

Windows 11 snaps a window to a half-screen by dragging it to the edge, or
`Win+Left` / `Win+Right`. Those are keyboard, so they are reliable; the drag is not.

### 2.6 Scrolling and what is visible

`page_find` and `ui_find` **scroll the element into view themselves** (`scroll_into_view`
defaults true). Do not pre-scroll to "look around" — it costs a round trip and the
re-measure already handles it.

When you must scroll a native app, `mouse_scroll` is real wheel input and is reliable
everywhere. Two measured traps:
- Scrolling with the pointer near a screen edge triggers shell gestures (GNOME opened the
  Activities overview mid-scroll). Keep the pointer inside the content area.
- `key_press("End")` does not scroll a web page in every app; use `mouse_scroll` with a
  large amount, or `pagedown`.

If the control you want is not found, it may simply be off-screen and not yet rendered.
That is a *scrolling* problem, not a *naming* problem — for a DOM page, `page_find` already
handles it; for a native app, scroll and re-run `ui_find`.

### 2.7 Hover-to-reveal, press-and-hold-to-reveal

These are real UI patterns that defeat naive clicking. Detect them and handle them
deliberately:

- **Hover reveals**: the element exists but has no visible affordance until the pointer
  arrives. `mouse_move` onto it *first*, then read `ui_find` again, then click. Hover
  almost always reveals a button, a row action, or a scrollbar.
- **Press and hold**: a long-press opens a context menu, starts a drag, or scrubs a
  slider. There is no tool for it. Use `mouse_drag` with several steps — the per-step gap
  is 12 ms, so 20 steps is a ~240 ms hold.
- **Right-click for a context menu**: there is no right-click parameter on `mouse_click`.
  `ui_invoke` (when the control has an Invoke pattern) or a keyboard context-menu key are
  the reliable routes.
- **Nested disclosure**: a row expands on click and its children then appear. Click, wait
  for the window to settle, re-read — do not assume the children were always there.

### 2.8 Drag, drop, select, resize

- `mouse_drag` sends press → moves → release with a real gap between them, which is what
  makes it work for anything that needs to see the events.
- Text selection: `ctrl+a` is universal. Then check it worked — if the app is a rich text
  control, a shortcut may do something else entirely.
- Window resize: drive the **title bar**, never the border. A border drag has a one-pixel
  hit target and the resize itself is a live loop that synthetic input handles badly.

### 2.9 Modal dialogs

A modal dialog on top **swallows every click** and every tool will still report success.
This is the single most expensive failure mode in this document, and it cost a real run:
a stray dialog appeared, four clicks went into it, and each one reported a successful
click.

Before acting, if `screen_state` shows a window that is not your target and covers your
point, stop and deal with it. `ui_click` warns; a raw `mouse_click` does not.

### 2.10 A surface that only lives a moment

Toasts, flyouts, the Start menu, notification banners, a `Ctrl+Shift+F` box that closes on
Escape. You cannot perceive these — the round trip is longer than their lifetime. **Batch
the whole interaction into one call and perceive only afterwards.**

---

## 3. Verify the page, never the URL

**A URL is a claim the page makes about itself. The rendered content is the fact.**

This is not a stylistic point — it cost a wrong answer on a real task. Navigating to what
looked like page 10 of a search engine and reading five results produced page 1's results,
and the only reason it was caught is that the result list looked too good for page one.

### 3.1 Jumping to page N by URL — get the parameter right

Search engines differ, and the off-by-one is silent: a wrong page still returns results, so
nothing fails, you just get the wrong five links.

| Engine | Parameter | Page 1 | Page 2 | **Page 10** |
| --- | --- | --- | --- | --- |
| Bing | `first` (1-based index of the first hit) | `first=1` | `first=11` | **`first=91`** |
| Google | `start` (0-based index) | `start=0` | `start=10` | `start=90` |

Bing's is 1-based and Google's is 0-based, so the two differ by one. `first=90` is **page 1**
on Bing; `start=90` is page 10 on Google.

**Prefer clicking the pager over building the URL** when the engine is reachable — the
pager cannot be off by one. Read the page numbers with `page_find` and click the one whose
text matches.

### 3.2 Confirm you are on the page you meant

After navigating, check something only the content can tell you — the result-count label,
a page-number `<span>`, the first and last result's text. On Bing that label reads
`91-100 of 6,910 results`, which is exactly the assertion you want and costs one
`page_find`.

```
page_find("span.sb_count")     # "91-100 of 6,910 results"  <- ground truth
```

If the label is absent, scroll to the pager and read which number is marked current.

### 3.3 After clicking, verify the destination

`page_click` returns `navigated`, which means **the href changed**. That is necessary and
not sufficient:

- **Redirect links often do not move the href.** Bing wraps every result in
  `bing.com/ck/a?...`, so a click can land on a new page while the address bar still
  shows the results. Verify with the *content* of the new page — its `h1`, its `title` —
  not with `navigated`.
- **A same-document change looks identical to a failure.** An SPA can rewrite the whole
  view without touching the URL. Read a heading and compare it to what you expected.

Report the destination by what you read on the page, and quote it. "The page's heading is
`Computer use | OpenAI API`" is evidence. "I clicked it" is not.

### 3.4 `dom-click` will not fire a redirect

`page_click` uses `how: dom-click` when the page is occluded — it calls `element.click()`
inside the page rather than synthesising a mouse event. That is the right default, but a
**redirect link will not follow it**: there is no navigation, because the redirect needs a
trusted gesture.

So: **bring the page to the front before clicking anything whose `href` is a redirect.** If
the result is `how: dom-click` and the link points at `bing.com/ck/a` (or any `/r/`,
`?url=`, `?u=` wrapper), raise the window and click again. Check `how` before you trust a
click that mattered.



### 3.5 Move the pointer before you press

This is the rule that turned WPS from "impossible" into "works".

```python
mouse_move(x - 30, y - 30)     # approach from outside the control
mouse_move(x, y)               # arrive: XAML hit-testing settles, hover appears
mouse_click(x, y)              # now it activates
```

Measured on WPS: `mouse_click` alone → hover appears, nothing happens. The same click
after a `mouse_move` → the New button opens. Before this rule, four separate WPS controls
all failed identically, and I had wrongly concluded the platform could not do it at all.

### 3.6 The window must actually be in front

`window_activate` returns `requested: true` when its last-resort reorder *ran*, and
`is_foreground: false` when the window still is not in front. **Check `is_foreground`** —
and note that it can be *stale*: measured, it reported `foreground_after: 0` while the very
next `screen_state` showed the window correctly in front. The shell settles a moment after
the call returns, so the reliable check is a fresh `screen_state`, not the activation
result.

`SetForegroundWindow` is refused to any process that does not own the foreground. When it
fails, **click the taskbar button** — real input, and it works.

**Read the taskbar button's position from a screenshot. Never hardcode it.** Icons shift as
apps open, close and pin, and a wrong guess lands on a different application entirely —
measured: a hardcoded coordinate hit Cốc Cốc instead of WPS, and the next guess hit
Chrome. A taskbar crop is ~680×50 pixels and costs one cheap image.

**Clicking a taskbar button may only open its thumbnail preview**, which is a window with
an empty title — it is not the switch. Verify the foreground after clicking, and if a
preview opened, move the pointer away to dismiss it and click again.

### 3.7 Never click a point you have not checked for overlap

`screen_state` returns window rectangles front-to-back. **Before clicking a coordinate,
check it is not inside the rectangle of a window that is in front of your target.** Two
windows routinely overlap — a 707×525 WPS home inside a 1300×983 browser — and the click
goes to the coverer while reporting success.

Check the point against the windows listed *before* your target in the list. If it is
covered, activate the target first and re-read, because activating may move it.

### 3.8 Re-measure immediately before the click

Between finding a control and clicking it, the window moves, the page reflows, a sticky
header grows. `ui_click` and `page_click` both re-read the rectangle immediately before
dispatching. If you take coordinates from a screenshot, the frame is already stale by the
time you act — see §5.

### 3.9 Verify that something changed

A click reporting success proves the click was *sent*. Use the cheap check:

```
page_state() or screen_state()   # did the url change, did the foreground change
```

For a navigation, `browser_click`/`page_click` does this for you and returns
`url_before` / `url_after` / `navigated` / `changed` — never a bare boolean, because an SPA
can change everything while the href stays the same.

---

## 4. Keystrokes

- `type_text` defaults to **no gap between characters**. A 40-character string costs
  **1.5 ms**; the old 8 ms default cost **625 ms**, because asyncio quantises every sleep
  to the 15.6 ms Windows timer tick. Ask for a gap only when typing into something that
  drops fast input.
- **Do not assume shortcuts exist.** `Ctrl+Shift+F` for the font box is a Word convention;
  WPS does not bind it, and the text you type afterwards lands in the document. Verify a
  shortcut did what you meant by checking the result.
- Gaps *between gestures* are load-bearing and must stay: the double-click interval, the
  per-step drag interval. They are what let the target process each event.

---

## 5. The one rule that matters most: batch, then perceive

A round trip through a model is **seconds**. Most of the tool costs here are **tens of
milliseconds**. So the tool is not your bottleneck — *you* are, and anything that makes you
decide between two actions is paying seconds for work that costs milliseconds.

```
DO:  one call  ->  key_press, type_text, key_press        (106 ms)
DON'T: call -> screenshot -> decide -> call -> decide     (seconds, and the first
                                                       frame is stale by the second)
```

This is why the Start menu cannot be automated by a model-driven loop, why a toast cannot,
and why measuring a control from a screenshot and then clicking it in a later turn is
always wrong.

**Perceive once per state change, not once per action.** Open → act → act → act →
verify. Not open → verify → act → verify → act.

---

## 6. Windows notes worth keeping

- **DPI is not optional.** The process is per-monitor-v2 aware, so all coordinates are
  *physical* pixels. A screenshot at native resolution maps 1:1. A scaled screenshot does
  not — multiply by the ratio, or measure with PIL, never by eye. Eyeballing was wrong by
  5–8% in testing.
- **Measure icon centres with a script**, not your eye. Cluster the non-background pixels
  in the band the row occupies; the click point is each cluster's centre. 52 ms, and it is
  exact.
- **The desktop is the virtual screen.** `SM_CXSCREEN` covers only the primary monitor; a
  second monitor to the left gives a negative origin, which is the space every Win32 call
  uses.
- **Out of reach, always:** the UAC secure desktop, and windows elevated above the agent's
  own process (UIPI). Run the client as the same user at the same integrity level as the
  apps you drive.
- **Minimised windows report a zero rectangle.** There is nothing to click. `ui_find`
  correctly returns nothing rather than inventing a point.

---

## 7. Two agents: one that thinks, one that does

Split the work when a task is long, repetitive, or pixel-heavy. Keep it in one process when
it is short — a subagent call is itself an inference plus a result read, and against a
1-second task that is a net loss.

Measured, on a five-step desktop task:

| | Tool calls | Screenshots | What the caller receives |
| --- | --- | --- | --- |
| Driven directly | 15 | 12 | every result and image |
| Delegated to an operator | 8 | **0** | one JSON object |

The operator is faster *perception*, not faster hands. It reuses the same tools.

### Division of labour

- **Agent 1 — the owner.** Holds the goal, decides what matters, and owns verification.
  It may not delegate judgement: it must check the operator's evidence before believing it.
- **Agent 2 — the operator.** Runs the loop. Decides *how* each step is performed —
  screenshot, `ui_find`, or `page_find` — within the scope it was given. It has its own
  context, so the screenshots never reach Agent 1.

Agent 2's job is to be cheap and literal, not clever. A low-effort model variant is right
for it: measured, it completed a task in **39.9 s** and, on a different task, **caught a
tool lying to it** and stopped rather than acting on a wrong window.

### The protocol

**Goal in.** Agent 1 sends a goal, a scope, and a deadline. Not a click list — a goal,
plus any facts it already knows (window handles, which selector worked last time).

**JSON out.** Agent 2's final message is one object:

```json
{
  "ok": true,
  "goal": "the goal as received",
  "result": "one sentence: what is true now that was not true before",
  "evidence": "the URL, the window title, or the value that was read",
  "url": "final url or null",
  "window": "foreground window title at the end, or null",
  "steps": 8,
  "screenshots": 0,
  "error": null,
  "notes": ["selectors that worked, what surprised you, what to do differently next time"]
}
```

`evidence` is **required when `ok` is true**. A claim without evidence is not a result, and
requiring it is what stops an operator from reporting success it did not observe.

**`notes` is the memory.** Agent 1 folds it into the next goal. This is the whole reason to
keep the same operator alive across goals rather than spawning a fresh one: it accumulates
which selector worked, which control only appears on hover, that a dialog blocks this
window.

### The conflict protocol — this is the part that matters

Two agents injecting input into one desktop will fight, and neither will notice. Four
rules prevent it.

1. **Single writer.** At most one agent injects input at a time. Agent 1 does not touch the
   mouse or keyboard while Agent 2 holds the lease, and says so in its own reasoning so the
   next actor waits.
2. **An explicit lease.** Agent 1 hands over a *named scope* — "the WPS window only", "the
   browser only" — not "the desktop". Agent 2 must not act outside it. If the goal needs
   the other window, it stops and reports rather than reaching for it.
3. **Escalate, never improvise.** If a step fails, Agent 2 stops and returns the step
   number and the error text. It must not try a different tool, retry, or fall back to
   pixel-clicking. A failed step is information for Agent 1; a silent workaround is not.
4. **Report the handover.** Agent 2's result states what it touched. Agent 1 re-reads
   `screen_state` before its next action rather than trusting its own memory of the desktop.

**The known collision.** opencode ships its own `tools.browser.*` namespace. An operator
saw it disconnected, concluded the working `page_find` was broken too, and abandoned a task
it could have finished. That is why these tools are named `page_*` and the operator is told
explicitly that another browser tool is not a substitute. If you write a similar prompt,
say it.

**Parse the result defensively.** Subagent output is prose; JSON inside it is a convention,
not a guarantee. A small free model completed the goal and then returned prose anyway. Take
the first `{` and try to parse, falling back to the last complete object.

---

## 8. Reporting back

Say what you did, what you checked, and what you could not verify. Three habits:

- **State the path you chose and why.** "Used `ui_find` because the app exposes its
  controls; a screenshot would have cost 1500 tokens for the same fact."
- **Flag the failures you hit.** A tool that reported a click that went nowhere is a finding
  the caller needs — otherwise they will trust the next one.
- **Never claim verification you did not perform.** If you confirmed a url, quote it. If
  you inferred it from a filename, say that instead.
