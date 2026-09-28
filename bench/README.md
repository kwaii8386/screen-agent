# Latency benchmark

Every millisecond figure quoted in this change, re-derived on the machine that runs it.

```bash
python bench/bench_latency.py            # full run, writes bench/latest.json
python bench/bench_latency.py --quick    # same measurements, 3 iterations, no warmup
python bench/bench_latency.py --no-json  # table to stdout only
python -m pytest tests/test_bench_latency.py   # checks the script is well-formed, not its numbers
```

Expect about **three minutes** for a full run. Most of it is the two slow measurements
(the pure-Python filter-1 decode and the sleep ladder), both of which exist because a
claim in the change description is about them.

## Why this is a script and not a test

A wall-clock assertion in `tests/` is a liability, not a check. It passes on a loaded
machine for the wrong reason, fails on a fast machine for the wrong reason, and it
keeps passing against code that has been made ten times slower if the machine happens
to be having a good day. The same 20 samples that make a median worth quoting make a
threshold worthless.

So the numbers live here, where a human or a nightly job runs them on purpose and
reads the spread, and `tests/test_bench_latency.py` only checks that this file is
well-formed and safe to run. It has **no millisecond assertion in it at all**, and it
says so about itself: `test_this_test_module_asserts_no_timing` fails if anyone adds
`perf_counter` to that test file.

`bench/` is outside pytest's collection. `pyproject.toml` sets
`testpaths = ["tests"]`, and `test_benchmark_is_outside_pytest_testpaths` asserts that
and that nothing in `tests/` contains `bench/`. Running plain `pytest` collects 255
tests, none of them from this directory.

## What it will not do

**It never injects input.** No `SendInput`, no key press, no typed string, no click,
no drag, no scroll, no pointer move, anywhere in the file.
`test_benchmark_source_has_no_input_injection` scans the source text for 22 call
patterns and fails the build if any of them appear - in code, in a docstring, in a
comment, or hidden behind an alias. The detector is itself exercised against eleven
known-bad snippets, so a green run cannot be green because the regex stopped matching.

It also launches nothing, kills nothing, attaches to nothing, and reads the desktop
only the way a screenshot does.

The consequence is that one quoted number is not measured, and the report says so in
its own "what was not measured" section rather than estimating it: the per-character
`SendInput` cost, and therefore the whole of the `1.47 ms` unpaced-typing figure.
Measuring it means typing into whatever window the user currently has focused. The
*pacing* loop around it is measured in full, and it turns out to cost microseconds,
which is itself the interesting part of that claim.

## Method

| | |
| --- | --- |
| iterations | 20 timed per measurement, 2 untimed warmup |
| timer | `time.perf_counter`, one pair per iteration |
| percentiles | nearest-rank, so a reported p95 is a real observed sample |
| statistics | median and p95, plus min and max, always |
| decomposition | end-to-end rows and their component rows are timed in the same process, on the same frames |
| injection | none; read-only against the desktop |

* **Why warmup.** The first GDI call, the first zlib call and the first `BitBlt` of a
  session all cost more than the hundredth. Timing them as samples puts a one-off cost
  in the median of everything.
* **Why nearest-rank.** An interpolated p95 is a number that never happened. For n=20
  the reported p95 is the 19th of 20 samples.
* **Why median and p95 both.** They answer different questions. If they are close, the
  run is quiet; if p95 is far above the median, something else was running, and that is
  more information than a mean would give.
* **The decomposition does not add up, and does not claim to.** Each part is timed in
  isolation with the previous output still referenced, while the total is timed inside
  one call. The measured ratio was 1.15x for the full screen and 1.24x for the region.
  Read the shares as *which step dominates*, not as an exact split. The dominance is
  not close and does not depend on the last few percent.

A quiet run and a contended run on the same machine, same code, same 20 iterations:

| | quiet | contended |
| --- | ---: | ---: |
| full-screen `capture_png` | 83.3 ms | 100.6 ms |
| GDI-scaled 640 | 33.0 ms | 41.5 ms |
| `capture_png` + `scale_image` | 116.6 ms | 133.0 ms |

Everything moved by the same ~20%. That is the argument for the medians: both runs
support the same conclusions, and a single min or a single sample would not have.

## Output

**stdout** gets a markdown report: the machine, the method, one table per area, a
facts section, and the list of what was not measured.

**`bench/latest.json`** gets the same run in a diffable form:

```json
{
  "schema": 1,
  "generated_utc": "2026-09-28T11:53:41Z",
  "machine":    { "cpu": "...", "cores_physical": 4, "cpus_logical": 8, "python": "3.11.9", ... },
  "method":     { "iterations": 20, "warmup": 2, "timer": "time.perf_counter", ... },
  "measurements": { "<name>": { "n": 20, "median": ..., "p95": ..., "min": ..., "max": ...,
                                "mean": ..., "stdev": ..., "status": "ok" } },
  "facts":      { "bitblt_fixed_stall": {...}, "timer_quantum": {...}, ... }
}
```

The machine block is not decoration. A latency number without the CPU, the core count
and the Python build it came from is not a fact, it is a rumour. `cores_physical` is
read from `GetLogicalProcessorInformationEx` and **validated** - the record layout is
self-calibrating and the answer is only accepted if the processor masks are disjoint,
non-zero and cover exactly the hardware threads Windows reports - because this runs in
a VM where the topology API can return a plausible lie, and a wrong core count is worse
than a missing one.

A CI job can diff `latest.json` against a recorded baseline and fail on a regression
without asserting a threshold in the test suite. A measurement that could not run is
recorded with `"status": "unavailable"` and a `reason`, never omitted and never zero.

## Measured baseline

Measured **2026-09-28** on an idle machine: 11th Gen Intel Core i7-1165G7 @ 2.80 GHz,
**4 physical cores / 8 hardware threads**, Intel Iris Xe, Windows 11 Pro (build
26200) at 125% scaling, Python 3.11.9, Pillow 12.1.1, 1920x1080 desktop, DPI awareness
`per-monitor-v2`. **n = 20** per row, 2 warmup, medians unless stated.

### 1. Full-screen capture, 1920x1080 -> PNG on disk

| step | median | p95 | share |
| --- | ---: | ---: | ---: |
| GDI grab (DC, `BitBlt`, `GetDIBits`, 8 MB `bytes()` copy) | 33.23 ms | 35.41 | 39.9% |
| BGRA -> RGB | 9.30 ms | 9.66 | 11.2% |
| PNG encode (scanline loop + zlib level 6) | 53.27 ms | 54.23 | 64.0% |
| ... of which `_idat` (zlib) | 53.29 ms | 53.86 | 100.0% of the encode |
| file write (132 KB) | 0.25 ms | 0.63 | 0.3% |
| **`capture_png` end to end** | **83.28 ms** | 85.51 | |

The share column sums to 115% because each part is timed in isolation and the total
inside one call - see *The decomposition does not add up* above. What is not close, and
does not depend on the last few percent, is that **zlib is the whole cost of the
encode** - the scanline-prefix loop is inside the noise of it - and that **the file
write is free**. So the only two things worth attacking on a full-screen capture are
the zlib level and the GDI grab, and the encode is the bigger of the two.

### 2. Region capture, 480x270

| step | median | p95 | share |
| --- | ---: | ---: | ---: |
| GDI grab | 16.49 ms | 18.02 | 98.7% |
| BGRA -> RGB | 0.36 ms | 0.49 | 2.2% |
| PNG encode | 3.69 ms | 3.92 | 22.1% |
| file write | 0.23 ms | 0.30 | 1.4% |
| **`capture_png` end to end** | **16.71 ms** | 17.51 | |

Shares sum to 124% for the same reason. With a 16x16 grab added as a floor:
**16.71 ms**. A region 506x the area of 16x16 pixels costs the same (0.99x). So there is
a real ~16.7 ms fixed cost in a `BitBlt` from the screen DC, and it is **50.3%** of the
full-screen grab.

### 3. Scaled capture to 640 wide

| path | median | p95 |
| --- | ---: | ---: |
| `capture_scaled(max_width=640)` - GDI `StretchBlt` | **33.04 ms** | 34.80 |
| `capture_png` + `core.scale_image(path, 640)` | **116.64 ms** | 119.27 |
| ... `scale_image` alone, Pillow available | 31.52 ms | 35.19 |
| ... `scale_image` alone, Pillow hidden, filter-0 input | 33.85 ms | 37.22 |
| ... `scale_image` alone, Pillow hidden, Pillow-written input | 825.93 ms | 841.23 |

Both paths produce **640x360** - asserted in the run, not assumed. The old path costs
**3.53x** more. On this machine `scale_image` took the Pillow branch; the last two rows
are the branch a bare install takes, measured by hiding Pillow from the import.

### 4. `downscale_rgb` 1920x1080 -> 640

| | value |
| --- | ---: |
| wall time | 6.74 ms (p95 7.18) |
| source bytes read | **2,073,600** |
| `new_h * stride` = 360 * 5,760 | 2,073,600 |
| ratio | **1.00x** |
| widest single read | 5,760 bytes (exactly one source row) |
| pre-fix `src[base:][:stride]`, reconstructed | **1,122,854,400** (1.05 GiB) = 542x |

### 5. Typing pacing and the timer quantum

| | value |
| --- | ---: |
| 40-character pacing loop, `interval=0.008` | **623.35 ms** (p95 625.69) |
| the same loop, `interval=0` (loop only) | **0.0028 ms** |
| timer quantum, floor of any non-zero `asyncio.sleep` | **15.566 ms** |
| `loop._clock_resolution` | **0.015625 s** |
| `asyncio.sleep(0)` (a yield) | 0.0044 ms |
| finest sleep honoured | 15 ms |

Ladder, requested against observed:

| requested | 0 | 0.1 ms | 1 ms | 4 ms | 8 ms | 12 ms | 15 ms | 15.6 ms | 16 ms | 32 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| observed | 0.004 | 15.60 | 15.73 | 15.57 | 15.65 | 15.64 | 16.31 | 29.80 | 29.38 | 45.52 |

Every request from 0.1 ms to 15 ms costs the same 15.6 ms. Asking for 15.6 ms buys a
second tick, not a shorter wait. 40 sleeps x 15.566 ms = 622.6 ms, which is the paced
number.

### 6. `screen_state` against a screenshot

| | median |
| --- | ---: |
| `screen_state()` | **0.2284 ms** (228 us) |
| ... the `EnumWindows` pass alone | 0.2295 ms |
| ... one window: title, class, rect | **0.0046 ms** (4.6 us) |
| cheapest possible screenshot (16x16, end to end) | 16.60 ms |
| region capture, 480x270 | 16.71 ms |
| full-screen capture | 83.28 ms |

### 7. Pure-Python PNG decode, 1920x1080

| | median | p95 | of which `_unfilter` |
| --- | ---: | ---: | ---: |
| filter 0 (what `imaging.encode_png` writes) | **11.62 ms** | 12.20 | - |
| filter 1 (Sub) | **527.08 ms** | 539.91 | - |
| a real Pillow PNG of the same frame | 807.68 ms | 823.03 | 794.46 ms (**98.4%**) |

All three decode back to the captured pixels exactly, so the fixtures are sound.

Filters Pillow chose for a screenshot of this desktop - this is the realistic case, and
it is worse than filter 1:

| filter | scanlines | share |
| --- | ---: | ---: |
| Sub (1) | 93 | 9% |
| Up (2) | 698 | 65% |
| Paeth (4) | 289 | 27% |

**100% of scanlines go through a per-byte Python loop**, and Paeth - three comparisons
and a branch per byte - is the most expensive of the three.

**For a user without Pillow.** `scale_image` on a bare install decodes the file it just
wrote, and `imaging.encode_png` writes filter 0, so `screenshot(scale=...)` costs
~34 ms and is fine. The 0.8 s cost arrives the moment the PNG came from somewhere else
- a browser, a pasted image, another screenshot tool - and then
`scale_image_640_no_pillow_pillow_png` measures **825.93 ms** end to end. That is the
argument for keeping the pure-Python decoder as a last resort that happens to be
correct, not as a fast path, and for not adding new callers to it.

## The claims, one by one

| # | claim | measured here | verdict |
| --- | --- | --- | --- |
| 1 | "a region costs about the same as a full screen because `BitBlt` has a fixed stall" | region 16.49 ms, full screen 33.23 ms, 16x16 16.71 ms | **REFUTED as stated.** The stall is real - a 480x270 grab costs 0.99x a 16x16 grab, and 50.3% of the full-screen grab is that same fixed cost - but a region is **half** a full-screen capture, not "about the same". The stall explains why shrinking below full-screen stops paying; it does not make a region free. |
| 2 | scaled capture "47 ms vs 146 ms" | 33.04 ms vs 116.64 ms (3.53x) | **Ratio CONFIRMED** (claimed 3.1x, both 640x360). **Absolute figures REFUTED on this machine**: both are ~20-30% lower than quoted. They match the `windows.py` docstring's "116 ms became 33 ms" to the millisecond, so the 47/146 pair looks like it came from a different machine or a colder run. |
| 3 | the row-read fix makes total bytes read "about `new_h * stride`" | 2,073,600 bytes vs 2,073,600 - **exactly 1.00x**, widest read one row | **CONFIRMED exactly.** |
| 4a | 40 chars at `interval=0.008` cost 625 ms | 623.35 ms (p95 625.69) | **CONFIRMED.** |
| 4b | asyncio quantises to the 15.625 ms Windows tick | floor of any non-zero sleep = 15.566 ms; `loop._clock_resolution` = 0.015625 s | **CONFIRMED.** |
| 4c | the same 40 chars at `interval=0` cost 1.47 ms | the pacing loop with no sleep costs 0.0028 ms | **NOT MEASURED, and not refuted.** The loop is free, so >=99.8% of the quoted 1.47 ms is the per-character `SendInput`, which this benchmark will not measure. Untested, not wrong. |
| 5 | `screen_state` is microseconds; a screenshot is tens of ms | `screen_state` 0.2284 ms; one window 0.0046 ms; cheapest screenshot 16.60 ms; full screen 83.28 ms | **Screenshot half CONFIRMED** (16.6-83 ms is tens of milliseconds). **"Microseconds" REFUTED for `screen_state`**: it is 228 us, ~228x "a microsecond", because it walks every top-level window. The `screen_state` docstring's narrower "GetForegroundWindow and GetWindowRect are about a microsecond each" is fair - one window's facts measures 4.6 us. The argument the number supports survives intact: state is 73x cheaper than the cheapest screenshot and 365x cheaper than a full one. |
| 6 | filter-1 decode 493 ms vs filter-0 8.5 ms, "all of it in the `_unfilter` loop" | filter 1 = 527.08 ms, filter 0 = 11.62 ms (45.4x); 98.4% of a real Pillow PNG's decode is inside `_unfilter` | **Mechanism CONFIRMED**, **absolute figures off by 7% and 37%** (claimed 493 and 8.5), and the ratio is 45x rather than 58x. Same order of magnitude, same conclusion. See the baseline above for what it means without Pillow. |

### One thing that is wrong in a comment

`imaging.py`'s `downscale_rgb` comment and `tests/test_imaging.py`'s
`test_downscale_row_slice_is_bounded` docstring both say the pre-fix expression
cost "~1.9 GB" of memcpy. The benchmark reconstructs that expression and counts the
bytes with the same `_RecordingBytes` technique those tests use: it is
**1,122,854,400 bytes - 1.12 GB / 1.05 GiB**, about 1.7x less than the comment claims.
The *direction* and the *severity* are right (542x the fixed loop, 180x the size of the
source buffer), the number is not. Both are in `src/` and `tests/`, which this change
does not touch, so it is reported here rather than fixed.

## Areas, and the measurement names behind them

| group | measures |
| --- | --- |
| `1-full-capture` | grab / convert / encode / write, and the total |
| `2-region-capture` | the same four for 480x270, plus a 16x16 floor |
| `3-scaled-capture` | GDI `StretchBlt` vs `capture_png` + `scale_image`, with and without Pillow |
| `4-downscale` | `downscale_rgb` wall time and source bytes read, plus the pre-fix reference |
| `5-type-pacing` | sleep quantum, the ladder, and both pacing intervals |
| `6-state-vs-screenshot` | `screen_state` and its parts, against the cheapest capture |
| `7-png-decode` | filter 0, filter 1, and a Pillow-written PNG |

`MEASUREMENT_NAMES` in `bench_latency.py` is the list, and
`test_required_measurements_are_all_present` pins the names that a drop would be
caught by. Adding a measurement is one `Bench(...)` in `BENCHES`; the tables, the JSON
and the shares all follow from the registry.

## Known limits of this benchmark

* **`GetDIBits` is not separable** from the rest of the GDI grab. Splitting it needs a
  change in `src/`, which this change does not make, so the grab row is reported whole
  and labelled as including it.
* **The physical core count can be absent.** It is only reported when the OS topology
  is self-consistent (see above). A null in the JSON means "not derivable here", not
  "unknown".
* **One machine.** Every number is a property of this CPU, this display and this
  desktop compositor. The JSON records the machine so a number is never quoted without
  it; a figure lifted out of this file onto a different box is not a measurement.
* **`EnumWindows` has a tail.** One sample of `screen_state` during development came in
  an order of magnitude above the median, which is why p95 and max are reported for
  every row rather than the median alone.
* **Windows real-time timer resolution can move the quantum.** 15.625 ms is this
  machine's default. A process that has already called `timeBeginPeriod`, a power plan
  that asks for a finer tick, or a different Windows build can all change it, which is
  exactly why the benchmark measures the quantum instead of hardcoding it.
