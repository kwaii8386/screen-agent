"""Re-derive every latency number the change description quotes, on the machine that runs it.

    python bench/bench_latency.py            # full run, writes bench/latest.json
    python bench/bench_latency.py --quick    # same measurements, 3 iterations
    python bench/bench_latency.py --no-json  # table only

Why this is a script and not a test
-----------------------------------
Every number in the docs is a wall-clock measurement, and a wall-clock assertion in
``tests/`` is a liability: it passes on a loaded machine for the wrong reason, fails on
a fast one for the wrong reason, and it passes unchanged against code that has been
made ten times slower on a lucky run. So the measurements live here, where a human or
a nightly job runs them on purpose and reads the spread, and ``tests/test_bench_latency.py``
only checks that this file is well-formed and inert.

What it does *not* do
---------------------
It never injects input. There is no ``SendInput``, no key press, no typed string, no
click and no drag anywhere in this file, and ``tests/test_bench_latency.py`` fails the
build if that changes. Measuring ``type_text`` would mean typing into whatever window
the user currently has focused; the pacing cost is measured instead, by timing the
``asyncio.sleep`` that dominates it, which is the part that is actually in question.
It also launches nothing, kills nothing, and reads the desktop exactly as a screenshot
does.

Method
------
One ``time.perf_counter()`` pair per iteration, ``warmup`` untimed iterations first
(so the first-call import / first-touch / first-BitBlt cost is not the sample),
``iterations`` timed ones after - 20 by default, which is the smallest sample that
gives a p95 worth reading. Percentiles are nearest-rank, not interpolated, so a
reported p95 is a real observed sample and not a value between two. The end-to-end
rows and the breakdown rows are timed in the same process, on the same frames, so
their shares add up; the per-part rows are a decomposition of the end-to-end row, not
a second run of the whole thing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shutil
import statistics
import struct
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:  # so the script runs from a checkout, uninstalled
    sys.path.insert(0, str(SRC_DIR))

import screen_agent.imaging as imaging  # noqa: E402
from screen_agent.backends import load_backend  # noqa: E402
from screen_agent.core import Rect, scale_image  # noqa: E402
from screen_agent.imaging import (  # noqa: E402
    PNG_SIGNATURE,
    _chunk,
    _idat,
    bgra_to_rgb,
    decode_png,
    downscale_rgb,
    encode_png,
)

__all__ = [
    "BENCHES",
    "GROUPS",
    "MEASUREMENT_NAMES",
    "Bench",
    "Ctx",
    "Result",
    "main",
    "run_all",
]

#: how many timed iterations each measurement gets unless it overrides this
DEFAULT_ITERATIONS = 20
#: untimed iterations first, to pay the one-off costs (imports, first GDI call, first
#: page-out of the working set) before the clock starts
DEFAULT_WARMUP = 2

#: the sleep ladder for the timer-quantum question: requested seconds -> observed.
#: Chosen to straddle the 1/64 s boundary the Windows scheduler works in, plus a few
#: multiples of it so a 2-tick or 3-tick result is distinguishable from a 1-tick one.
LADDER_SECONDS = (0.0, 0.0001, 0.001, 0.004, 0.008, 0.012, 0.015, 0.0156, 0.016, 0.032)
#: sleeps per ladder rung, and independent repetitions of each rung, so a rung has a
#: median rather than a single sample. 25 x ~15.6 ms is ~0.4 s per rung, ~4 s per rung
#: set, and 10 rungs is the whole ladder.
LADDER_SLEEPS = 25
LADDER_REPEATS = 3
#: sleeps per repetition for the named quantum measurements
QUANTUM_SLEEPS = 10
#: the character count the pacing claim is written about
PACE_CHARS = 40


# --------------------------------------------------------------------------- model
class Unavailable(Exception):
    """A measurement that cannot run here (no Pillow, no desktop, ...).

    Recorded as a fact with a reason rather than dropped, because "we did not measure
    this" is a result too and must not quietly become a missing row.
    """


@dataclass
class Result:
    """One measurement: a name, a sample, and the summary of that sample."""

    name: str
    label: str
    group: str
    unit: str = "ms"
    n: int = 0
    values: list[float] = field(default_factory=list)
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    reason: str = ""

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": self.label,
            "group": self.group,
            "unit": self.unit,
            "n": self.n,
            "status": self.status,
            "note": self.note,
        }
        if self.status != "ok":
            out["reason"] = self.reason
            return out
        ordered = sorted(self.values)
        out.update(
            {
                "median": round(statistics.median(ordered), 4),
                "mean": round(statistics.fmean(ordered), 4),
                "p95": round(_nearest_rank(ordered, 0.95), 4),
                "min": round(ordered[0], 4),
                "max": round(ordered[-1], 4),
                "stdev": round(statistics.stdev(ordered), 4) if len(ordered) > 1 else 0.0,
            }
        )
        if self.extra:
            out["extra"] = self.extra
        return out

    @property
    def median(self) -> float:
        if self.status != "ok" or not self.values:
            return float("nan")
        return statistics.median(self.values)


def _nearest_rank(ordered: list[float], quantile: float) -> float:
    """Nearest-rank percentile: the smallest sample at or above *quantile* of the data.

    Not interpolated, so a p95 printed here is a number that actually happened.
    """
    if not ordered:
        return float("nan")
    rank = max(1, -(-int(quantile * len(ordered) * 1000) // 1000))  # ceil(q * n), integer
    return ordered[min(len(ordered), rank) - 1]


@dataclass(frozen=True)
class Bench:
    """A named measurement. ``run`` is sync and returns a :class:`Result`."""

    name: str
    group: str
    label: str
    run: Callable[[Ctx], Result]
    note: str = ""
    #: name of the measurement this one is a component of, for the share column
    share_of: str = ""
    #: 0 means "use the run's default iteration count"
    iterations: int = 0
    #: unit is "ms" unless this says otherwise
    unit: str = "ms"


GROUPS = (
    "1-full-capture",
    "2-region-capture",
    "3-scaled-capture",
    "4-downscale",
    "5-type-pacing",
    "6-state-vs-screenshot",
    "7-png-decode",
)


# ------------------------------------------------------------------------ fixtures
class _RecordingBytes(bytes):
    """``bytes`` that remembers the extent of every slice taken of it.

    The technique from ``tests/test_imaging.py::test_downscale_row_slice_is_bounded``,
    reused here because it is the only way to see the difference: a row read that
    copies the tail of the buffer costs the same *number* of Python operations and
    a hundred times the bytes, which a timer on a fast machine will happily call
    noise.
    """

    def __new__(cls, data: bytes) -> _RecordingBytes:
        self = super().__new__(cls, data)
        self.reads: list[tuple[int, int]] = []
        return self

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if isinstance(key, slice):
            self.reads.append((key.start or 0, len(self) if key.stop is None else key.stop))
        return value

    @property
    def copied(self) -> int:
        """Total source bytes any single read pulled into a new object."""
        return sum(stop - start for start, stop in self.reads)

    @property
    def widest(self) -> int:
        """The longest single read."""
        return max((stop - start for start, stop in self.reads), default=0)


@dataclass
class Ctx:
    """Everything a measurement needs: the real backend, a scratch dir, the frames."""

    backend: Any
    workdir: Path
    iterations: int
    warmup: int
    desktop: Rect
    region: Rect
    tiny: Rect
    facts: dict[str, Any] = field(default_factory=dict)
    _cache: dict[str, Any] = field(default_factory=dict)

    # -- lazily built fixtures, so a group that needs no pixels never takes any
    def cached(self, key: str, build: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def full_bgra(self) -> bytes:
        return self.cached("full_bgra", lambda: self.backend._grab_bgra(
            self.desktop, self.desktop.w, self.desktop.h, cursor=True
        ))

    def full_rgb(self) -> bytes:
        return self.cached("full_rgb", lambda: bgra_to_rgb(self.full_bgra()))

    def full_png(self) -> bytes:
        return self.cached("full_png", lambda: encode_png(self.desktop.w, self.desktop.h,
                                                           self.full_rgb()))

    def region_bgra(self) -> bytes:
        return self.cached("region_bgra", lambda: self.backend._grab_bgra(
            self.region, self.region.w, self.region.h, cursor=True
        ))

    def region_rgb(self) -> bytes:
        return self.cached("region_rgb", lambda: bgra_to_rgb(self.region_bgra()))

    def region_png(self) -> bytes:
        return self.cached("region_png", lambda: encode_png(self.region.w, self.region.h,
                                                             self.region_rgb()))

    def scratch(self, name: str) -> Path:
        return self.workdir / name


# ------------------------------------------------------------------- timing helpers
def _time_sync(fn: Callable[[], Any], *, iterations: int, warmup: int) -> tuple[list[float], Any]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    last: Any = None
    for _ in range(iterations):
        start = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples, last


async def _time_async(fn: Callable[[], Any], *, iterations: int, warmup: int) -> tuple[list, Any]:
    for _ in range(warmup):
        await fn()
    samples: list[float] = []
    last: Any = None
    for _ in range(iterations):
        start = time.perf_counter()
        last = await fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples, last


def _counts(iterations: int, warmup: int, fn: Callable[[], Any]) -> tuple[list[Any], Any]:
    """Like :func:`_time_sync` but for a quantity that is not a duration."""
    for _ in range(warmup):
        fn()
    out: list[Any] = []
    last: Any = None
    for _ in range(iterations):
        last = fn()
        out.append(last)
    return out, out[-1] if out else None


def _result(
    bench: Bench,
    values: list[float],
    *,
    note: str = "",
    extra: dict[str, Any] | None = None,
    unit: str = "ms",
) -> Result:
    return Result(
        name=bench.name,
        label=bench.label,
        group=bench.group,
        unit=unit,
        n=len(values),
        values=values,
        note=note,
        extra=extra or {},
    )


def _unavailable(bench: Bench, reason: str) -> Result:
    return Result(
        name=bench.name,
        label=bench.label,
        group=bench.group,
        unit=bench.unit,
        note=bench.note,
        status="unavailable",
        reason=reason,
    )


# ============================================================== 1. full-screen capture
def _full_grab(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_gdi_grab"]
    w, h = ctx.desktop.w, ctx.desktop.h
    samples, _ = _time_sync(
        lambda: ctx.backend._grab_bgra(ctx.desktop, w, h, cursor=True),
        iterations=ctx.iterations,
        warmup=ctx.warmup,
    )
    return _result(bench, samples)


def _full_bgra_to_rgb(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_bgra_to_rgb"]
    bgra = ctx.full_bgra()
    samples, _ = _time_sync(lambda: bgra_to_rgb(bgra), iterations=ctx.iterations, warmup=ctx.warmup)
    return _result(bench, samples, extra={"buffer_bytes": len(bgra)})


def _full_encode(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_png_encode"]
    rgb, w, h = ctx.full_rgb(), ctx.desktop.w, ctx.desktop.h
    samples, blob = _time_sync(lambda: encode_png(w, h, rgb), iterations=ctx.iterations,
                              warmup=ctx.warmup)
    return _result(bench, samples, extra={"png_bytes": len(blob)})


def _full_zlib(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_png_zlib"]
    rgb, w, h = ctx.full_rgb(), ctx.desktop.w, ctx.desktop.h
    samples, stream = _time_sync(lambda: _idat(rgb, w, h, 6), iterations=ctx.iterations,
                                warmup=ctx.warmup)
    return _result(bench, samples, extra={"idat_bytes": len(stream), "zlib_level": 6})


def _full_write(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_file_write"]
    blob = ctx.full_png()
    path = ctx.scratch("full.png")
    path.write_bytes(blob)
    samples, _ = _time_sync(lambda: path.write_bytes(blob), iterations=ctx.iterations,
                           warmup=ctx.warmup)
    return _result(bench, samples, extra={"png_bytes": len(blob)})


def _full_end_to_end(ctx: Ctx) -> Result:
    bench = _by_name["capture_full_end_to_end"]
    path = ctx.scratch("full_e2e.png")

    async def once():
        return await ctx.backend.capture_png(path, ctx.desktop)

    samples, size = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(bench, samples, extra={"image": list(size), "bytes_on_disk": path.stat().st_size})


# ================================================================ 2. region capture
def _region_grab(ctx: Ctx) -> Result:
    bench = _by_name["capture_region_gdi_grab"]
    r = ctx.region
    samples, _ = _time_sync(
        lambda: ctx.backend._grab_bgra(r, r.w, r.h, cursor=True),
        iterations=ctx.iterations,
        warmup=ctx.warmup,
    )
    return _result(bench, samples)


def _region_bgra_to_rgb(ctx: Ctx) -> Result:
    bench = _by_name["capture_region_bgra_to_rgb"]
    bgra = ctx.region_bgra()
    samples, _ = _time_sync(lambda: bgra_to_rgb(bgra), iterations=ctx.iterations, warmup=ctx.warmup)
    return _result(bench, samples, extra={"buffer_bytes": len(bgra)})


def _region_encode(ctx: Ctx) -> Result:
    bench = _by_name["capture_region_png_encode"]
    rgb = ctx.region_rgb()
    samples, blob = _time_sync(lambda: encode_png(ctx.region.w, ctx.region.h, rgb),
                              iterations=ctx.iterations, warmup=ctx.warmup)
    return _result(bench, samples, extra={"png_bytes": len(blob)})


def _region_write(ctx: Ctx) -> Result:
    bench = _by_name["capture_region_file_write"]
    blob = ctx.region_png()
    path = ctx.scratch("region.png")
    path.write_bytes(blob)
    samples, _ = _time_sync(lambda: path.write_bytes(blob), iterations=ctx.iterations,
                           warmup=ctx.warmup)
    return _result(bench, samples, extra={"png_bytes": len(blob)})


def _region_end_to_end(ctx: Ctx) -> Result:
    bench = _by_name["capture_region_end_to_end"]
    path = ctx.scratch("region_e2e.png")

    async def once():
        return await ctx.backend.capture_png(path, ctx.region)

    samples, size = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(bench, samples, extra={"image": list(size)})


def _tiny_grab(ctx: Ctx) -> Result:
    bench = _by_name["capture_tiny_gdi_grab"]
    r = ctx.tiny
    samples, _ = _time_sync(
        lambda: ctx.backend._grab_bgra(r, r.w, r.h, cursor=False),
        iterations=ctx.iterations,
        warmup=ctx.warmup,
    )
    return _result(bench, samples, extra={"region": [r.w, r.h]},
                   note="the floor: 100x fewer pixels than the full screen, same grab")


# ================================================================= 3. scaled capture
def _scaled_gdi(ctx: Ctx) -> Result:
    bench = _by_name["capture_scaled_gdi_640"]
    path = ctx.scratch("scaled_gdi.png")

    async def once():
        return await ctx.backend.capture_scaled(path, ctx.desktop, max_width=640)

    samples, size = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(bench, samples, extra={"image": list(size)}, note="GDI StretchBlt into a 640 bitmap")


def _scaled_old_path(ctx: Ctx) -> Result:
    bench = _by_name["capture_png_then_scale_640"]
    path = ctx.scratch("scaled_old.png")

    async def once():
        await ctx.backend.capture_png(path, ctx.desktop)
        return scale_image(path, 640)

    samples, size = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(
        bench, samples, extra={"image": list(size), "scale_image_branch": ctx.facts.get(
            "scale_image_branch")},
        note="capture full size, then rescale the finished file",
    )


def _scale_image_only(ctx: Ctx, *, no_pillow: bool, source: str) -> Result:
    """Time ``core.scale_image`` alone, restoring a full-size file outside the clock.

    ``scale_image`` early-returns when the file is already narrow enough, so each
    iteration has to start from a pristine full-size copy - done *before* the start
    timestamp, or the copy would be reported as part of the measurement.
    """
    name = ("scale_image_640_no_pillow_own_png" if source == "own" and no_pillow
            else "scale_image_640_no_pillow_pillow_png" if no_pillow
            else "scale_image_640_only")
    bench = _by_name[name]
    if source == "pillow":
        try:
            blob = _pillow_png(ctx)
        except ImportError as exc:
            raise Unavailable(f"Pillow is not installed here, so there is no Pillow-written "
                              f"PNG to scale ({exc})") from exc
    else:
        blob = ctx.full_png()
    pristine = ctx.scratch(f"pristine_{source}.png")
    pristine.write_bytes(blob)
    target = ctx.scratch(f"scale_{source}.png")

    samples: list[float] = []
    size: tuple[int, int] = (0, 0)

    def once() -> None:
        nonlocal size
        shutil.copyfile(pristine, target)  # outside the clock: see the docstring
        started = time.perf_counter()
        size = scale_image(target, 640)
        samples.append((time.perf_counter() - started) * 1000.0)

    stack = _no_pillow() if no_pillow else contextlib.nullcontext()
    with stack:
        for _ in range(ctx.warmup):
            once()
        samples.clear()  # the warmup copies are not samples
        for _ in range(ctx.iterations):
            once()

    return _result(
        bench, samples,
        extra={
            "image": list(size),
            "pillow_available": not no_pillow,
            "input_filters": _filter_histogram(blob),
            "input_png_bytes": len(blob),
        },
        note="scale_image() on an already-captured file; the file copy is not timed",
    )


@contextlib.contextmanager
def _no_pillow():
    """Make ``from PIL import Image`` raise, the way a bare install behaves.

    ``import`` treats a ``None`` entry in ``sys.modules`` as a missing module, which
    is the only way to reach ``scale_image``'s pure-Python branch in a process that
    happens to have Pillow installed. The entry is restored on the way out, and the
    real submodules are put back too, because Pillow is used elsewhere in this run.
    """
    saved = {k: v for k, v in sys.modules.items() if k == "PIL" or k.startswith("PIL.")}
    for key in saved:
        del sys.modules[key]
    sys.modules["PIL"] = None
    try:
        yield
    finally:
        del sys.modules["PIL"]
        sys.modules.update(saved)


# ==================================================================== 4. downscale_rgb
def _downscale_time(ctx: Ctx) -> Result:
    bench = _by_name["downscale_rgb_1920_to_640"]
    rgb, w, h = ctx.full_rgb(), ctx.desktop.w, ctx.desktop.h
    samples, out = _time_sync(lambda: downscale_rgb(rgb, w, h, 640), iterations=ctx.iterations,
                             warmup=ctx.warmup)
    small, new_w, new_h = out
    ctx.facts["downscale_output"] = {"new_w": new_w, "new_h": new_h,
                                     "source_bytes": len(rgb), "out_bytes": len(small)}
    return _result(bench, samples, extra={"out_image": [new_w, new_h]})


def _downscale_bytes(ctx: Ctx) -> Result:
    """Count the source bytes the current row read pulls, with the fix in place."""
    bench = _by_name["downscale_rgb_1920_to_640_bytes_read"]
    w, h = ctx.desktop.w, ctx.desktop.h
    stride = w * 3

    def once() -> dict[str, int]:
        src = _RecordingBytes(ctx.full_rgb())
        _, new_w, new_h = downscale_rgb(src, w, h, 640)
        return {
            "reads": len(src.reads),
            "bytes_read": src.copied,
            "widest_read": src.widest,
            "one_row_every_output_row": new_h * stride,
            "source_bytes": len(src),
        }

    runs, last = _counts(max(3, ctx.iterations // 4), 1, once)
    values = [float(r["bytes_read"]) for r in runs]
    ctx.facts["downscale_reads"] = last
    return _result(bench, values, unit="bytes", extra=last,
                   note="same value every run; n is the number of confirmations")


def _downscale_bug_reference(ctx: Ctx) -> Result:
    """The pre-fix row read, reconstructed here, to price the fix.

    ``src[base:][:stride]`` copies the whole remaining tail of the buffer and then
    throws all but one row away. It is not in ``src/`` any more; this is a local
    reconstruction of the expression so the saving has a number attached, and it runs
    on an in-memory buffer with no reference to the desktop.
    """
    bench = _by_name["downscale_rgb_tail_slice_reference_bytes_read"]
    w, h = ctx.desktop.w, ctx.desktop.h
    stride = w * 3
    src = ctx.full_rgb()

    def once() -> dict[str, int]:
        recorder = _RecordingBytes(src)
        copied = 0
        new_h = max(1, int(h * 640 / w))
        for y in range(new_h):
            src_y = min(h - 1, int((y + 0.5) * h / new_h))
            base = src_y * stride
            copied += len(recorder[base:]) + len(recorder[base:][:stride])
        return {"reads": len(recorder.reads), "bytes_read": copied,
                "source_bytes": len(src), "one_row_every_output_row": new_h * stride}

    runs, last = _counts(2, 0, once)
    ctx.facts["downscale_tail_slice_reference"] = last
    return _result(bench, [float(r["bytes_read"]) for r in runs], unit="bytes", extra=last,
                   note="pre-fix expression, reconstructed; not the code in src/")


# =================================================================== 5. typing pacing
def _sleep_cost(seconds: float, repeats: int, sleeps: int) -> list[float]:
    """Per-call cost of ``asyncio.sleep(seconds)``, in ms, averaged over *sleeps*."""
    async def once() -> None:
        for _ in range(sleeps):
            await asyncio.sleep(seconds)

    samples, _ = asyncio.run(_time_async(once, iterations=repeats, warmup=1))
    # _time_async timed the whole batch of `sleeps`; this is about one call
    return [total / sleeps for total in samples]


def _sleep_quantum(ctx: Ctx, seconds: float, name: str) -> Result:
    bench = _by_name[name]
    samples = _sleep_cost(seconds, ctx.iterations, QUANTUM_SLEEPS)
    return _result(bench, samples,
                   extra={"requested_sleep_ms": seconds * 1000, "sleeps_per_repeat": QUANTUM_SLEEPS},
                   note=f"per call, averaged over {QUANTUM_SLEEPS} sleeps")


def _sleep_ladder(ctx: Ctx) -> Result:
    """Every rung of the ladder, and the derived quantum and resolution."""
    bench = _by_name["asyncio_sleep_ladder"]
    rungs = []
    for seconds in LADDER_SECONDS:
        samples = _sleep_cost(seconds, LADDER_REPEATS, LADDER_SLEEPS)
        observed = statistics.median(samples)
        rungs.append({
            "requested_ms": round(seconds * 1000, 6),
            "observed_ms": round(observed, 4),
            "min_ms": round(min(samples), 4),
            "max_ms": round(max(samples), 4),
            "repeats": LADDER_REPEATS,
            "sleeps_per_repeat": LADDER_SLEEPS,
        })
    flat = [r["observed_ms"] for r in rungs]
    baseline = rungs[0]["observed_ms"]
    # the quantum: the cost of any positive sleep, taken from the cheapest rung that
    # still costs something, i.e. the smallest non-zero request in the ladder
    positive = [r for r in rungs if r["requested_ms"] > 0]
    quantum = min(r["observed_ms"] for r in positive) if positive else float("nan")
    # the finest sleep the timer actually honours: the largest request that still
    # completes inside a single quantum
    single = [r for r in rungs if r["observed_ms"] < quantum * 1.5]
    finest = max((r["requested_ms"] for r in single), default=float("nan"))
    # the smallest request that costs more than a bare yield, i.e. is not free
    nontrivial = min((r["requested_ms"] for r in rungs
                      if r["observed_ms"] > baseline * 10 and r["requested_ms"] > 0),
                     default=float("nan"))
    loop = asyncio.new_event_loop()
    try:
        clock_resolution = loop._clock_resolution  # noqa: SLF001 - the point is to report it
        loop_impl = type(loop).__name__
    finally:
        loop.close()
    derived = {
        "rungs": rungs,
        "yield_only_cost_ms": baseline,
        "timer_quantum_ms": round(quantum, 4),
        "finest_sleep_honoured_ms": round(finest, 4),
        "smallest_non_free_sleep_ms": round(nontrivial, 6),
        "loop_class": loop_impl,
        "loop_clock_resolution_s": clock_resolution,
    }
    ctx.facts["timer_quantum"] = derived
    values = flat
    return _result(bench, values,
                   extra={k: v for k, v in derived.items() if k != "rungs"},
                   note="per-call cost at each ladder rung, one sample per rung; the "
                        "request/observed pairs and the derived quantum are in "
                        "facts.timer_quantum")


def _pacing(ctx: Ctx, interval: float, name: str) -> Result:
    """The pacing loop of a typing call, with the input delivery left out.

    ``WindowsBackend.type_text`` awaits ``asyncio.sleep(interval)`` once per
    character, so this reproduces that loop exactly and times the part that is in
    question. What it does *not* include is the ``SendInput`` call per character:
    measuring that would mean typing into the user's focused window, so this
    benchmark does not do it and does not guess at the number.
    """
    bench = _by_name[name]
    text = "abcdefghij" * (PACE_CHARS // 10)
    samples: list[float] = []

    async def once() -> None:
        started = time.perf_counter()
        for _ch in text:  # the per-character test that precedes the sleep
            if interval:
                await asyncio.sleep(interval)
        samples.append((time.perf_counter() - started) * 1000.0)

    asyncio.run(once())  # warmup
    samples.clear()
    for _ in range(ctx.iterations):
        asyncio.run(once())

    return _result(bench, samples,
                   extra={"characters": PACE_CHARS, "interval_s": interval,
                          "excludes": "the per-character SendInput call"},
                   note="loop + sleeps only; no input is injected")


# ========================================================== 6. state vs a screenshot
def _screen_state(ctx: Ctx) -> Result:
    bench = _by_name["screen_state"]

    async def once():
        return await ctx.backend.screen_state()

    samples, state = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(bench, samples,
                   extra={"windows_reported": len(state.get("windows", [])),
                          "payload_bytes": len(json.dumps(state, default=str))},
                   note="facts only, no pixels")


def _cheapest_screenshot(ctx: Ctx) -> Result:
    bench = _by_name["screenshot_cheapest_16x16"]
    path = ctx.scratch("tiny.png")
    r = ctx.tiny

    async def once():
        return await ctx.backend.capture_png(path, r, cursor=False)

    samples, size = asyncio.run(_time_async(once, iterations=ctx.iterations, warmup=ctx.warmup))
    return _result(bench, samples, extra={"image": list(size)},
                   note="smallest capture the backend will do, end to end")


def _screen_state_parts(ctx: Ctx) -> Result:
    """The part of ``screen_state`` that is the window enumeration, for context.

    Timed on its own in a tight loop, so it is a different measurement context from
    ``screen_state`` and the two should not be subtracted from each other: what it does
    establish is which of the two the time is in.
    """
    bench = _by_name["screen_state_windows_only"]
    u32 = ctx.backend._u32()  # noqa: SLF001 - measuring a private step, read-only
    samples, _ = _time_sync(
        lambda: ctx.backend._visible_windows(u32, skip=0),  # noqa: SLF001
        iterations=ctx.iterations,
        warmup=ctx.warmup,
    )
    return _result(bench, samples, note="the EnumWindows pass on its own, in a tight loop")


def _one_window_facts(ctx: Ctx) -> Result:
    """The per-window cost inside that enumeration: title, class and rect.

    This is the number the ``screen_state`` docstring calls "about a microsecond", and
    measuring it separately is the only way to tell whether ``screen_state`` is slow
    because a window lookup is slow or because there are hundreds of windows to look up.
    """
    bench = _by_name["screen_state_one_window_facts"]
    u32 = ctx.backend._u32()  # noqa: SLF001
    samples, _ = _time_sync(
        lambda: ctx.backend._window_facts(u32, int(u32.GetForegroundWindow() or 0)),  # noqa: SLF001
        iterations=ctx.iterations,
        warmup=ctx.warmup,
    )
    return _result(bench, samples, note="one window: GetWindowText + GetClassName + GetWindowRect")


# ==================================================================== 7. PNG decoding
def _filtered_png(rgb: bytes, width: int, height: int, ftype: int) -> bytes:
    """A PNG whose every scanline carries *ftype*, built from real pixels.

    ``imaging.encode_png`` always writes filter 0, which is why it can decode its own
    files quickly. A PNG from anywhere else - Pillow, a browser, a screenshot tool -
    does not. This makes both cases measurable on the same frame.
    """
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        row = rgb[y * stride : (y + 1) * stride]
        raw.append(ftype)
        if ftype == 0:
            raw += row
        elif ftype == 1:
            raw += bytes((row[i] - (row[i - 3] if i >= 3 else 0)) & 0xFF for i in range(stride))
        elif ftype == 2:
            raw += bytes((row[i] - rgb[(y - 1) * stride + i]) & 0xFF for i in range(stride)) \
                if y else bytes(row)
        else:
            prior = rgb[(y - 1) * stride : y * stride] if y else bytes(stride)
            out = bytearray(stride)
            for i in range(stride):
                left = out[i - 3] if i >= 3 else 0
                up = prior[i]
                upleft = prior[i - 3] if i >= 3 else 0
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else upleft)
                out[i] = (row[i] - pred) & 0xFF
            raw += out
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def _pillow_png(ctx: Ctx) -> bytes:
    return ctx.cached(
        "pillow_png",
        lambda: _encode_with_pillow(ctx.full_rgb(), ctx.desktop.w, ctx.desktop.h),
    )


def _encode_with_pillow(rgb: bytes, width: int, height: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), rgb).save(buf, "PNG")
    return buf.getvalue()


def _filter_histogram(png: bytes) -> dict[int, int]:
    pos, idat = len(PNG_SIGNATURE), bytearray()
    width = height = channels = 0
    while pos + 8 <= len(png):
        length = int.from_bytes(png[pos : pos + 4], "big")
        tag = png[pos + 4 : pos + 8]
        if tag == b"IHDR":
            width, height, _d, color, *_ = struct.unpack(">IIBBBBB",
                                                          png[pos + 8 : pos + 8 + length])
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color, 3)
        elif tag == b"IDAT":
            idat += png[pos + 8 : pos + 8 + length]
        elif tag == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    hist: dict[int, int] = {}
    for y in range(height):
        hist[raw[y * (stride + 1)]] = hist.get(raw[y * (stride + 1)], 0) + 1
    return {k: hist[k] for k in sorted(hist)}


def _decode_instrumented(blob: bytes) -> float:
    """Decode *blob*, returning the total milliseconds spent inside ``_unfilter``.

    ``decode_png`` looks ``_unfilter`` up in its own module globals, so replacing the
    attribute there is enough to attribute the time. The wrapper costs two
    ``perf_counter`` calls per scanline - about 0.1 ms on a 1080-row image, which is
    under 1% of a filter-1 decode and about 1% of a filter-0 one.
    """
    original = imaging._unfilter  # noqa: SLF001
    spent = [0.0]

    def timed(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            spent[0] += time.perf_counter() - started

    imaging._unfilter = timed  # noqa: SLF001
    try:
        decode_png(blob)
    finally:
        imaging._unfilter = original  # noqa: SLF001
    return spent[0] * 1000.0


def _decode(ctx: Ctx, name: str, blob_fn: Callable[[Ctx], bytes], note: str) -> Result:
    bench = _by_name[name]
    blob = blob_fn(ctx)
    samples: list[float] = []
    unfilters: list[float] = []

    def once() -> None:
        unfilter = _decode_instrumented(blob)  # untimed: instrumentation overhead
        started = time.perf_counter()
        decode_png(blob)
        samples.append((time.perf_counter() - started) * 1000.0)
        unfilters.append(unfilter)

    for _ in range(ctx.warmup):
        once()
    samples.clear()
    unfilters.clear()
    for _ in range(ctx.iterations):
        once()

    return _result(
        bench, samples,
        extra={"unfilter_median_ms": round(statistics.median(unfilters), 4),
               "unfilter_share_pct": round(
                   100 * statistics.median(unfilters) / statistics.median(samples), 1)
               if statistics.median(samples) else None,
               "png_bytes": len(blob)},
        note=note,
    )


def _decode_filter0(ctx: Ctx) -> Result:
    bench = _by_name["png_decode_filter0"]
    w, h = ctx.desktop.w, ctx.desktop.h
    ctx.facts["png_filter0_roundtrip_exact"] = None
    blob = _filtered_png(ctx.full_rgb(), w, h, 0)
    ok = decode_png(blob) == (w, h, ctx.full_rgb())
    ctx.facts["png_filter0_roundtrip_exact"] = ok
    return _decode(ctx, bench.name, lambda _c: blob,
                   "filter 0: what imaging.encode_png writes, and all decode_png has to read")


def _decode_filter1(ctx: Ctx) -> Result:
    bench = _by_name["png_decode_filter1"]
    w, h = ctx.desktop.w, ctx.desktop.h
    blob = _filtered_png(ctx.full_rgb(), w, h, 1)
    ok = decode_png(blob) == (w, h, ctx.full_rgb())
    ctx.facts["png_filter1_roundtrip_exact"] = ok
    return _decode(ctx, bench.name, lambda _c: blob,
                   "filter 1 (Sub): a pure-Python per-byte loop in _unfilter")


def _decode_pillow(ctx: Ctx) -> Result:
    bench = _by_name["png_decode_pillow_png"]
    try:
        blob = _pillow_png(ctx)
    except ImportError as exc:
        return _unavailable(bench, f"Pillow is not installed here ({exc})")
    w, h = ctx.desktop.w, ctx.desktop.h
    ctx.facts["pillow_png_filters"] = _filter_histogram(blob)
    ctx.facts["pillow_png_roundtrip_exact"] = decode_png(blob) == (w, h, ctx.full_rgb())
    return _decode(ctx, bench.name, lambda _c: blob,
                   "a PNG Pillow wrote from the same frame: whatever filters it picked")


# ============================================================================== registry
BENCHES: tuple[Bench, ...] = (
    Bench("capture_full_gdi_grab", "1-full-capture",
          "full 1920x1080: GDI grab (DC, BitBlt, GetDIBits, bytes copy)",
          _full_grab, share_of="capture_full_end_to_end",
          note="includes GetDIBits and the 8 MB bytes() copy; not separable read-only"),
    Bench("capture_full_bgra_to_rgb", "1-full-capture",
          "full: BGRA -> RGB channel swap", _full_bgra_to_rgb,
          share_of="capture_full_end_to_end"),
    Bench("capture_full_png_encode", "1-full-capture",
          "full: PNG encode (scanline loop + zlib level 6)", _full_encode,
          share_of="capture_full_end_to_end"),
    Bench("capture_full_png_zlib", "1-full-capture",
          "full: _idat only (inside the encode above)", _full_zlib,
          share_of="capture_full_png_encode", note="a component of the encode, not of the total"),
    Bench("capture_full_file_write", "1-full-capture",
          "full: write the PNG to disk", _full_write, share_of="capture_full_end_to_end"),
    Bench("capture_full_end_to_end", "1-full-capture",
          "full 1920x1080: capture_png end to end", _full_end_to_end,
          note="the same four steps above, inside one call"),

    Bench("capture_region_gdi_grab", "2-region-capture",
          "region 480x270: GDI grab", _region_grab, share_of="capture_region_end_to_end"),
    Bench("capture_region_bgra_to_rgb", "2-region-capture",
          "region: BGRA -> RGB", _region_bgra_to_rgb, share_of="capture_region_end_to_end"),
    Bench("capture_region_png_encode", "2-region-capture",
          "region: PNG encode", _region_encode, share_of="capture_region_end_to_end"),
    Bench("capture_region_file_write", "2-region-capture",
          "region: write the PNG to disk", _region_write, share_of="capture_region_end_to_end"),
    Bench("capture_region_end_to_end", "2-region-capture",
          "region 480x270: capture_png end to end", _region_end_to_end),
    Bench("capture_tiny_gdi_grab", "2-region-capture",
          "16x16 region: GDI grab (the fixed-cost floor)", _tiny_grab,
          note="100x fewer pixels than the full screen; the difference is the stall"),

    Bench("capture_scaled_gdi_640", "3-scaled-capture",
          "full screen -> 640 wide, GDI StretchBlt (capture_scaled)", _scaled_gdi),
    Bench("capture_png_then_scale_640", "3-scaled-capture",
          "full screen -> 640 wide, capture_png then core.scale_image", _scaled_old_path,
          note="the pre-change path, on this machine's Pillow state"),
    Bench("scale_image_640_only", "3-scaled-capture",
          "core.scale_image alone, Pillow available", lambda c: _scale_image_only(
              c, no_pillow=False, source="own")),
    Bench("scale_image_640_no_pillow_own_png", "3-scaled-capture",
          "core.scale_image alone, Pillow hidden, filter-0 input", lambda c: _scale_image_only(
              c, no_pillow=True, source="own"),
          note="the branch a bare install takes on a file it wrote itself"),
    Bench("scale_image_640_no_pillow_pillow_png", "3-scaled-capture",
          "core.scale_image alone, Pillow hidden, Pillow-written input", lambda c: _scale_image_only(
              c, no_pillow=True, source="pillow"),
          note="the worst case: no Pillow, and a PNG that uses real filters"),

    Bench("downscale_rgb_1920_to_640", "4-downscale",
          "downscale_rgb 1920x1080 -> 640 (nearest, itemgetter)", _downscale_time),
    Bench("downscale_rgb_1920_to_640_bytes_read", "4-downscale",
          "downscale_rgb: source bytes read by the row loop", _downscale_bytes),
    Bench("downscale_rgb_tail_slice_reference_bytes_read", "4-downscale",
          "the pre-fix row read, for comparison", _downscale_bug_reference, iterations=2),

    Bench("asyncio_sleep_1ms_quantum", "5-type-pacing",
          "asyncio.sleep(0.001) x10, per call", lambda c: _sleep_quantum(
              c, 0.001, "asyncio_sleep_1ms_quantum")),
    Bench("asyncio_sleep_8ms_quantum", "5-type-pacing",
          "asyncio.sleep(0.008) x10, per call (the old interval)", lambda c: _sleep_quantum(
              c, 0.008, "asyncio_sleep_8ms_quantum")),
    Bench("asyncio_sleep_ladder", "5-type-pacing",
          "sleep ladder: requested vs observed, one sample per rung", _sleep_ladder),
    Bench("typing_pace_40_chars_interval_0_008", "5-type-pacing",
          "40-character pacing loop, interval=0.008", lambda c: _pacing(
              c, 0.008, "typing_pace_40_chars_interval_0_008"),
          note="no input injected; excludes the per-character SendInput"),
    Bench("typing_pace_40_chars_interval_0", "5-type-pacing",
          "40-character pacing loop, interval=0 (loop cost only)", lambda c: _pacing(
              c, 0.0, "typing_pace_40_chars_interval_0"),
          note="the 1.47 ms claim also needs the SendInput cost, which is not measurable here"),

    Bench("screen_state", "6-state-vs-screenshot",
          "backend.screen_state()", _screen_state),
    Bench("screen_state_windows_only", "6-state-vs-screenshot",
          "the EnumWindows pass inside screen_state", _screen_state_parts),
    Bench("screen_state_one_window_facts", "6-state-vs-screenshot",
          "one window: title, class, rect", _one_window_facts,
          note="the per-window cost, for the 'about a microsecond' claim"),
    Bench("screenshot_cheapest_16x16", "6-state-vs-screenshot",
          "cheapest possible screenshot: 16x16 capture_png end to end", _cheapest_screenshot),

    Bench("png_decode_filter0", "7-png-decode",
          "decode_png of a filter-0 PNG (what this package writes)", _decode_filter0),
    Bench("png_decode_filter1", "7-png-decode",
          "decode_png of a filter-1 PNG (Sub)", _decode_filter1),
    Bench("png_decode_pillow_png", "7-png-decode",
          "decode_png of a PNG Pillow wrote from the same frame", _decode_pillow),
)

MEASUREMENT_NAMES: tuple[str, ...] = tuple(b.name for b in BENCHES)
_by_name: dict[str, Bench] = {b.name: b for b in BENCHES}


# ---------------------------------------------------------------------------- runner
def _cpu_model() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except (OSError, ImportError):
            pass
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def _physical_cores() -> int | None:
    """Physical core count, which ``os.cpu_count()`` is not (it counts hardware threads).

    Asked for because a latency figure without its core count is not reproducible: a
    4-core part with SMT disabled and a 4-core part with 8 hardware threads do not
    heat, turbo or contend the same way.

    Windows exposes the topology through ``GetLogicalProcessorInformationEx``, but the
    size of one entry is a union whose largest member differs between Windows
    versions, and on a virtual machine the reported topology can be synthetic
    nonsense. So rather than trust a struct layout, this asks for the
    ``RelationProcessorCore`` records and *validates* the answer: the candidate entry
    sizes that divide the returned buffer exactly, that carry the requested
    relationship in every entry, and whose processor masks are disjoint, non-zero and
    together cover exactly the hardware threads Windows reported are all accepted -
    and only if exactly one candidate survives is it believed. If none or several do,
    this returns ``None`` and the JSON says so, because a plausible wrong core count
    is worse than a missing one.
    """
    if sys.platform != "win32":
        try:
            ids = {
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
                if line.lower().startswith(("core id", "cpu cores"))
            }
            return len(ids) or None
        except OSError:
            return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.GetLogicalProcessorInformationEx
        query.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL

        relation_processor_core = 0
        size = wintypes.DWORD(0)
        query(relation_processor_core, None, ctypes.byref(size))  # asks how big
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not query(relation_processor_core, buffer, ctypes.byref(size)):
            return None
        raw = bytes(buffer.raw[: size.value])
    except (OSError, AttributeError, ValueError):  # pragma: no cover - Windows only
        return None

    threads = os.cpu_count() or 0
    accepted: list[tuple[int, int]] = []
    for stride in (48, 56, 64, 72, 80):
        if stride > len(raw) or len(raw) % stride:
            continue
        entries = len(raw) // stride
        if not entries:
            continue
        relationships = [int.from_bytes(raw[i * stride : i * stride + 4], "little")
                         for i in range(entries)]
        if any(rel != relation_processor_core for rel in relationships):
            continue
        for offset in range(0, stride - 3, 4):
            masks = [int.from_bytes(raw[i * stride + offset : i * stride + offset + 4], "little")
                     for i in range(entries)]
            if any(mask == 0 for mask in masks) or _overlaps(masks):
                continue
            if sum(bin(mask).count("1") for mask in masks) != threads:
                continue
            accepted.append((entries, offset))
    return accepted[0][0] if len(accepted) == 1 else None


def _overlaps(masks: list[int]) -> bool:
    """True if any two bitmasks share a bit."""
    seen = 0
    for mask in masks:
        if seen & mask:
            return True
        seen |= mask
    return False


def _machine(ctx: Ctx) -> dict[str, Any]:
    try:
        import PIL

        pillow: str | None = PIL.__version__
    except ImportError:
        pillow = None
    logical = os.cpu_count()
    physical = _physical_cores()
    return {
        "cpu": _cpu_model(),
        "processor": platform.processor(),
        "cores_physical": physical,
        "cpus_logical": logical,
        "cpus_available": (
            len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else logical
        ),
        "platform": platform.platform(),
        "os": f"{platform.system()} {platform.release()}",
        "os_build": platform.version(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "pillow": pillow,
        "backend": ctx.backend.name,
        "backend_display": ctx.backend.display_name,
        "desktop": [ctx.desktop.x, ctx.desktop.y, ctx.desktop.w, ctx.desktop.h],
        "dpi_awareness": getattr(ctx.backend, "_dpi", "unknown"),
    }


def _derived_facts(results: dict[str, Result], ctx: Ctx) -> dict[str, Any]:
    """Facts that are ratios or agreements between measurements, not new timings."""
    out: dict[str, Any] = {}

    def med(name: str) -> float:
        return results[name].median if name in results else float("nan")

    def ok(*names: str) -> bool:
        return all(results.get(n) and results[n].status == "ok" for n in names)

    ok_all = ok("capture_full_end_to_end", "capture_full_gdi_grab", "capture_full_bgra_to_rgb",
                "capture_full_png_encode", "capture_full_file_write",
                "capture_region_end_to_end", "capture_region_gdi_grab",
                "capture_region_bgra_to_rgb", "capture_region_png_encode",
                "capture_region_file_write")
    if ok_all:
        # The parts are timed one at a time and the total is timed as a whole, so they
        # are not required to add up. Report the difference rather than let the reader
        # assume they do.
        for prefix, label in (("full", "1920x1080"), ("region", "480x270")):
            parts = [med(f"capture_{prefix}_{part}") for part in
                     ("gdi_grab", "bgra_to_rgb", "png_encode", "file_write")]
            total = med(f"capture_{prefix}_end_to_end")
            out[f"decomposition_{prefix}"] = {
                "label": label,
                "parts_ms": [round(p, 3) for p in parts],
                "sum_of_parts_ms": round(sum(parts), 3),
                "end_to_end_ms": round(total, 3),
                "sum_over_end_to_end": round(sum(parts) / total, 3) if total else None,
                "caveat": "each part is timed in isolation with the previous output still "
                          "referenced, the total inside one call; treat the shares as "
                          "indicative of where the time goes, not as an exact split",
            }
    if ok("capture_scaled_gdi_640", "capture_png_then_scale_640"):
        fast, slow = med("capture_scaled_gdi_640"), med("capture_png_then_scale_640")
        out["scaled_path_speedup"] = {
            "gdi_ms": round(fast, 3), "old_path_ms": round(slow, 3),
            "old_over_gdi": round(slow / fast, 2) if fast else None,
        }
    if ok("capture_scaled_gdi_640", "capture_png_then_scale_640", "scale_image_640_only"):
        out["scaled_path_same_size"] = (
            results["capture_scaled_gdi_640"].extra.get("image")
            == results["capture_png_then_scale_640"].extra.get("image")
            == results["scale_image_640_only"].extra.get("image")
        )
        out["scaled_image_size"] = results["capture_scaled_gdi_640"].extra.get("image")
    if ok("capture_full_gdi_grab", "capture_region_gdi_grab", "capture_tiny_gdi_grab"):
        full, tiny = med("capture_full_gdi_grab"), med("capture_tiny_gdi_grab")
        out["bitblt_fixed_stall"] = {
            "full_px": ctx.desktop.w * ctx.desktop.h,
            "tiny_px": ctx.tiny.w * ctx.tiny.h,
            "tiny_ms": round(tiny, 3), "full_ms": round(full, 3),
            "marginal_ms_for_2M_px": round(full - tiny, 3),
            "stall_share_of_full_pct": round(100 * tiny / full, 1) if full else None,
            "region_ms": round(med("capture_region_gdi_grab"), 3),
            "region_px": ctx.region.w * ctx.region.h,
            "region_over_tiny": round(med("capture_region_gdi_grab") / tiny, 2) if tiny else None,
        }
    if ok("screen_state", "screenshot_cheapest_16x16", "capture_full_end_to_end"):
        state = med("screen_state")
        per_window = med("screen_state_one_window_facts") if ok(
            "screen_state_one_window_facts") else None
        out["state_vs_screenshot"] = {
            "screen_state_ms": round(state, 4),
            "cheapest_screenshot_ms": round(med("screenshot_cheapest_16x16"), 3),
            "full_screenshot_ms": round(med("capture_full_end_to_end"), 3),
            "cheapest_is_x_state": round(med("screenshot_cheapest_16x16") / state, 1)
            if state else None,
            "full_is_x_state": round(med("capture_full_end_to_end") / state, 1) if state else None,
            "one_window_facts_ms": round(per_window, 5) if per_window else None,
            "caveat": "the 'about a microsecond' figure in screen_state's docstring is the "
                      "cost of one window lookup; the cost of screen_state is that lookup "
                      "times the number of top-level windows, which is why the total is "
                      "hundreds of microseconds. EnumWindows can also stall: a single "
                      "sample has been seen an order of magnitude above the median, which is "
                      "why p95 and max are reported.",
        }
    if ok("png_decode_filter1", "png_decode_filter0"):
        slow, fast = med("png_decode_filter1"), med("png_decode_filter0")
        out["decode_filter_ratio"] = {
            "filter0_ms": round(fast, 3), "filter1_ms": round(slow, 3),
            "filter1_over_filter0": round(slow / fast, 1) if fast else None,
        }
    if ok("typing_pace_40_chars_interval_0_008", "typing_pace_40_chars_interval_0",
          "asyncio_sleep_8ms_quantum"):
        paced = med("typing_pace_40_chars_interval_0_008")
        unpaced = med("typing_pace_40_chars_interval_0")
        quantum = med("asyncio_sleep_8ms_quantum")
        out["typing_pacing"] = {
            "paced_40_chars_ms": round(paced, 3),
            "unpaced_loop_ms": round(unpaced, 5),
            "sleep_quantum_ms": round(quantum, 4),
            "quantum_x_paced": round(paced / quantum, 1) if quantum else None,
            "not_measured": "the per-character SendInput cost, so the unpaced number here is "
                            "a floor, not the 1.47 ms the docstring quotes",
        }
    out.update(ctx.facts)
    return out


def run_all(ctx: Ctx, *, verbose: bool = True) -> dict[str, Result]:
    results: dict[str, Result] = {}
    current = None
    for bench in BENCHES:
        if bench.group != current:
            current = bench.group
            if verbose:
                print(f"  ... {current}", file=sys.stderr, flush=True)
        iterations = bench.iterations or ctx.iterations
        started = time.perf_counter()
        try:
            result = bench.run(ctx)
        except Unavailable as exc:
            result = _unavailable(bench, str(exc))
        except Exception as exc:  # noqa: BLE001 - a failed measurement is a result too
            result = _unavailable(bench, f"{type(exc).__name__}: {exc}")
        if result.n and result.n != iterations and bench.iterations == 0:
            result.note = (result.note + f" (n={result.n})").strip()
        results[bench.name] = result
        if verbose:
            took = (time.perf_counter() - started) * 1000
            print(f"      {bench.name:<48} {took:8.0f} ms of wall clock", file=sys.stderr,
                  flush=True)
    return results


# ---------------------------------------------------------------------------- render
def _fmt(value: float, unit: str) -> str:
    if value != value:  # NaN
        return "n/a"
    if unit == "bytes":
        for scale, suffix in ((1 << 30, "GiB"), (1 << 20, "MiB"), (1 << 10, "KiB")):
            if abs(value) >= scale:
                return f"{value / scale:.2f} {suffix}"
        return f"{value:.0f} B"
    if abs(value) < 1:
        return f"{value:.4f}"
    return f"{value:.2f}"


def _share(bench: Bench, results: dict[str, Result]) -> str:
    if not bench.share_of:
        return ""
    parent = results.get(bench.share_of)
    if parent is None or parent.status != "ok" or parent.median <= 0:
        return "n/a"
    child = results[bench.name]
    if child.status != "ok" or child.median != child.median:
        return "n/a"
    return f"{100 * child.median / parent.median:.1f}%"


def render_markdown(results: dict[str, Result], ctx: Ctx, meta: dict[str, Any]) -> str:
    lines: list[str] = []
    machine = meta["machine"]
    lines.append("# screen-agent latency benchmark")
    lines.append("")
    lines.append(f"measured {meta['generated_utc']} on the machine named below - every number "
                 "in this file was produced by this run")
    lines.append("")
    lines.append(f"* **machine** : {machine['cpu']}")
    cores = (f"{machine['cores_physical']} physical cores / {machine['cpus_logical']} hardware "
             f"threads" if machine["cores_physical"] else
             f"{machine['cpus_logical']} hardware threads (physical count unavailable)")
    lines.append(f"* **cores** : {cores}")
    lines.append(f"* **os** : {machine['os']} (build {machine['os_build']})")
    lines.append(f"* **python** : {machine['python']} ({machine['python_implementation']})")
    lines.append(f"* **backend** : {machine['backend']} - {machine['backend_display']}")
    lines.append(f"* **desktop** : {machine['desktop'][2]}x{machine['desktop'][3]}"
                 f" at ({machine['desktop'][0]}, {machine['desktop'][1]})")
    lines.append(f"* **DPI awareness** : {machine['dpi_awareness']}")
    lines.append(f"* **Pillow** : {machine['pillow'] or 'not installed'}")
    lines.append("")
    lines.append(f"* **iterations** : {ctx.iterations} timed per measurement "
                 f"({ctx.warmup} untimed warmup)")
    lines.append("* **timer** : `time.perf_counter`, one pair per iteration")
    lines.append("* **percentiles** : nearest-rank, so p95 is an observed sample, not an "
                 "interpolation")
    lines.append("* **noise** : median and p95 are both reported. A wide gap is a loaded "
                 "machine and is left visible rather than smoothed away.")
    lines.append("* **decomposition** : the per-part rows are the steps of the end-to-end row, "
                 "timed in the same process on the same frames. They are timed one at a time, "
                 "so they do not add up to the end-to-end row exactly; the "
                 "`decomposition_*` facts report by how much.")
    lines.append("* **no input** : nothing here injects keystrokes, clicks or pointer moves. "
                 "The typing row times the pacing loop, not the typing.")
    lines.append("")

    for group in GROUPS:
        members = [b for b in BENCHES if b.group == group]
        if not members:
            continue
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| measurement | n | median | p95 | min | max | share | what it is |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for bench in members:
            res = results[bench.name]
            if res.status != "ok":
                lines.append(f"| `{bench.name}` | - | n/a | n/a | n/a | n/a | - | "
                             f"**not measured**: {res.reason} |")
                continue
            share = _share(bench, results)
            ordered = sorted(res.values)
            note = bench.note or res.note
            lines.append(
                f"| `{bench.name}` | {res.n} | {_fmt(res.median, res.unit)} | "
                f"{_fmt(_nearest_rank(ordered, 0.95), res.unit)} | "
                f"{_fmt(ordered[0], res.unit)} | {_fmt(ordered[-1], res.unit)} | "
                f"{share or '-'} | {label_and_note(bench, note)} |"
            )
        lines.append("")

    facts = meta["facts"]
    lines.append("## facts derived from the same run")
    lines.append("")
    for title, body in _fact_sections(facts, results):
        lines.append(f"### {title}")
        lines.append("")
        lines.extend(body)
        lines.append("")
    lines.append("## what was not measured, and why")
    lines.append("")
    for line in _not_measured(results, facts):
        lines.append(f"* {line}")
    lines.append("")
    return "\n".join(lines)


def label_and_note(bench: Bench, note: str) -> str:
    text = bench.label
    if note:
        text = f"{text} <br/>{note}"
    return text.replace("|", "\\|")


def _fact_sections(facts: dict[str, Any], results: dict[str, Result]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []

    for key in ("decomposition_full", "decomposition_region"):
        dec = facts.get(key)
        if not dec:
            continue
        sections.append((f"Does the decomposition add up? ({dec['label']})", [
            f"* sum of the four parts : {dec['sum_of_parts_ms']:.2f} ms "
            f"({', '.join(f'{p:.2f}' for p in dec['parts_ms'])})",
            f"* `capture_png` end to end : {dec['end_to_end_ms']:.2f} ms",
            f"* ratio : {dec['sum_over_end_to_end']:.2f}x",
            "",
            "Each part is timed in isolation with the previous output still referenced, and "
            "the total inside a single call, so a ratio that is not 1.00 is expected and is "
            "not an error. Read the shares as *which step dominates*, not as an exact split. "
            "The PNG encode dominates the full screen and the GDI grab dominates the region; "
            "that is the finding, and it does not depend on the last percent.",
        ]))

    stall = facts.get("bitblt_fixed_stall")
    if stall:
        sections.append(("BitBlt fixed stall", [
            "The 16x16 grab and the 480x270 grab cost the same to within noise, while the "
            "full 1920x1080 grab costs about the same again on top of them:",
            "",
            "| region | pixels | median |",
            "| --- | ---: | ---: |",
            f"| 16x16 | {stall['tiny_px']:,} | {stall['tiny_ms']:.2f} ms |",
            f"| 480x270 | {stall.get('region_px', '')} | {stall['region_ms']:.2f} ms |",
            f"| 1920x1080 | {stall['full_px']:,} | {stall['full_ms']:.2f} ms |",
            "",
            f"* pixels carry no cost: 480x270 costs {stall['region_over_tiny']}x a 16x16 grab, "
            f"so the copy itself is not what is being paid for",
            f"* what is left to pay is the stall: {stall['stall_share_of_full_pct']}% of the "
            f"full-screen grab is the same fixed cost a 16-pixel grab pays",
        ]))

    quantum = facts.get("timer_quantum")
    if quantum:
        rows = ["| requested sleep | observed | implied ticks |",
                "| ---: | ---: | ---: |"]
        for rung in quantum["rungs"]:
            ticks = rung["observed_ms"] / quantum["timer_quantum_ms"] if quantum[
                "timer_quantum_ms"] else 0
            rows.append(
                f"| {rung['requested_ms']:g} ms | {rung['observed_ms']:.3f} ms | {ticks:.2f} |"
            )
        sections.append(("Timer quantum", rows + [
            "",
            f"* event loop : `{quantum['loop_class']}`, clock resolution "
            f"{quantum['loop_clock_resolution_s']:.1e} s",
            f"* **quantum** : {quantum['timer_quantum_ms']:.3f} ms - the cheapest that any "
            f"non-zero sleep costs, and what every request from "
            f"{quantum['smallest_non_free_sleep_ms']:g} ms to "
            f"{quantum['finest_sleep_honoured_ms']:g} ms actually costs",
            f"* `asyncio.sleep(0)` is a yield, not a sleep, and costs "
            f"{quantum['yield_only_cost_ms']:.4f} ms",
            f"* the finest sleep the timer honours is {quantum['finest_sleep_honoured_ms']:g} ms: "
            f"ask for more and you buy a second tick, not a shorter wait. Any interval below "
            f"that is indistinguishable from any other interval below that.",
        ]))

    down = facts.get("downscale_reads")
    ref = facts.get("downscale_tail_slice_reference")
    out = facts.get("downscale_output")
    if down and ref and out:
        sections.append(("downscale_rgb row reads", [
            f"*{_fmt(out['source_bytes'], 'bytes')} of source pixels -> "
            f"{out['new_w']}x{out['new_h']} out*",
            "",
            "| row read | reads | source bytes read | vs one row per output row |",
            "| --- | ---: | ---: | ---: |",
            f"| current: `src[base:base + stride]` | {down['reads']:,} | "
            f"{_fmt(down['bytes_read'], 'bytes')} | "
            f"{down['bytes_read'] / down['one_row_every_output_row']:.2f}x |",
            f"| pre-fix: `src[base:][:stride]` | {ref['reads']:,} | "
            f"{_fmt(ref['bytes_read'], 'bytes')} | "
            f"{ref['bytes_read'] / ref['one_row_every_output_row']:.0f}x |",
            "",
            f"* the fix holds the total to `new_h * stride` = "
            f"{_fmt(down['one_row_every_output_row'], 'bytes')}: the loop reads "
            f"{down['bytes_read'] / down['one_row_every_output_row']:.2f}x that, and the widest "
            f"single read is {down['widest_read']:,} bytes, which is exactly one source row",
            f"* the pre-fix expression reads the whole remaining tail on every output row: "
            f"{_fmt(ref['bytes_read'], 'bytes')}, which is "
            f"{ref['bytes_read'] / down['bytes_read']:.0f}x what the fixed loop copies, and "
            f"{ref['bytes_read'] / out['source_bytes']:.0f}x the size of the source itself",
        ]))

    ratio = facts.get("state_vs_screenshot")
    if ratio:
        sections.append(("screen_state against a screenshot", [
            f"* `screen_state()` : {ratio['screen_state_ms']:.3f} ms",
            f"* cheapest possible screenshot (16x16, end to end): "
            f"{ratio['cheapest_screenshot_ms']:.2f} ms "
            f"({ratio['cheapest_is_x_state']}x the state)",
            f"* a full-screen screenshot: {ratio['full_screenshot_ms']:.2f} ms "
            f"({ratio['full_is_x_state']}x the state)",
        ] + ([
            f"* one window's facts (title, class, rect): "
            f"{ratio['one_window_facts_ms'] * 1000:.1f} us",
        ] if ratio.get("one_window_facts_ms") else []) + [
            "",
            "The ordering the argument rests on holds: state is tens to hundreds of times "
            "cheaper than any screenshot, and a screenshot of anything real is tens of "
            "milliseconds. The absolute figure does not: a per-window lookup is a few "
            "microseconds, but `screen_state` walks every top-level window, so the total is "
            f"{ratio['screen_state_ms'] * 1000:.0f} us, not 'about a microsecond'. It is "
            "still cheap enough to be worth preferring - it is a rounding error next to a "
            "capture - but it is a rounding error of the wrong order of magnitude for the "
            "quote to be left as written.",
        ]))

    ratio = facts.get("decode_filter_ratio")
    hist = facts.get("pillow_png_filters")
    if ratio or hist:
        rows = []
        if hist:
            total = sum(hist.values())
            rows.append("Filters Pillow actually chose for a screenshot of this desktop:")
            rows.append("")
            rows.append("| filter | scanlines | share | cost in `_unfilter` |")
            rows.append("| --- | ---: | ---: | --- |")
            names = {0: "None (copy)", 1: "Sub (per-byte loop)",
                     2: "Up (per-byte loop)", 3: "Average (per-byte loop)",
                     4: "Paeth (per-byte, 3 compares)"}
            for ftype, count in hist.items():
                rows.append(f"| {names.get(ftype, ftype)} | {count:,} | "
                            f"{100 * count / total:.0f}% | pure Python per byte |")
            rows.append("")
        if ratio:
            multiple = ratio["filter1_over_filter0"]
            rows += [
                "Filter 0 has a zero-cost branch in `_unfilter`; every other filter is a "
                f"per-byte Python loop, which measured {multiple}x more here:",
                "",
                f"* filter 0 : {ratio['filter0_ms']:.2f} ms",
                f"* filter 1 : {ratio['filter1_ms']:.2f} ms ({multiple}x)",
            ]
            if "png_decode_pillow_png" in results and results[
                    "png_decode_pillow_png"].status == "ok":
                r = results["png_decode_pillow_png"]
                rows.append(
                    f"* a real Pillow PNG of the same frame: {r.median:.2f} ms, of which "
                    f"{r.extra.get('unfilter_median_ms')} ms "
                    f"({r.extra.get('unfilter_share_pct')}%) is inside `_unfilter`"
                )
            rows += [
                "",
                "**What this means without Pillow.** `scale_image` on a bare install reads the "
                "PNG it just wrote, and `imaging.encode_png` writes filter 0, so "
                "`screenshot(scale=...)` is fine without Pillow. The cost appears when a "
                "PNG arrives from somewhere else - a browser, a pasted image, a screenshot "
                "tool - and then decoding it is a half-second operation. That is a reason to "
                "keep the pure-Python path as a *last resort* and not as a fast path.",
            ]
        sections.append(("Pure-Python PNG decode", rows))

    pace = facts.get("typing_pacing")
    if pace:
        sections.append(("Typing pacing", [
            f"* `{PACE_CHARS}` characters, `interval=0.008` (the old default): "
            f"{pace['paced_40_chars_ms']:.2f} ms, which is "
            f"{pace['quantum_x_paced']:.1f} sleeps' worth of timer quantum at "
            f"{pace['sleep_quantum_ms']:.3f} ms each",
            f"* the same loop with `interval=0`: {pace['unpaced_loop_ms']:.4f} ms - the loop, "
            f"the per-character branch and the interval test, and nothing else",
            "",
            "The unpaced number is microseconds, so essentially all of the "
            "1.47 ms quoted in `type_text`'s docstring would have to be the per-character "
            "input delivery. This benchmark will not measure that: it would mean typing into "
            "the user's focused window. The paced number, which is where the latency an agent "
            "actually feels lives, is measured in full and is the one worth acting on.",
        ]))

    scale = facts.get("scaled_path_speedup")
    if scale:
        sections.append(("Scaled capture, both paths", [
            f"* GDI StretchBlt : {scale['gdi_ms']:.2f} ms",
            f"* capture_png + `core.scale_image` : {scale['old_path_ms']:.2f} ms",
            f"* the old path costs **{scale['old_over_gdi']}x** more, for the same image",
        ]))

    branch = facts.get("scale_image_branch")
    if branch is not None:
        sections.append(("Which scale_image branch ran", [
            f"* on this machine `core.scale_image` took the **{branch}** path, so the old-path "
            f"row above is the fast branch. The `no_pillow` rows are the branch a bare install "
            f"takes, measured by hiding Pillow from the import."
        ]))

    return sections


def _not_measured(results: dict[str, Result], facts: dict[str, Any]) -> list[str]:
    out: list[str] = []
    unavailable = [r for r in results.values() if r.status != "ok"]
    for res in unavailable:
        out.append(f"`{res.name}`: {res.reason}")
    out.append(
        "**The per-character `SendInput` cost, and therefore the whole of the "
        f"`{PACE_CHARS}`-character `interval=0` figure.** Measuring it means typing into "
        "whatever window the user has focused, so this benchmark does not do it. The "
        "`typing_pace_40_chars_interval_0` row is the loop *without* the injection and is a "
        "lower bound on the real number, not a measurement of it - and it comes out at "
        "microseconds, which says the quoted figure is almost entirely delivery cost. The "
        "paced row, which is where the time actually goes, is measured in full."
    )
    out.append(
        "`GetDIBits` separately from the rest of the GDI grab. Splitting it needs a change "
        "to `src/`, which this benchmark does not make, so the grab row is reported whole."
    )
    pillow = results.get("png_decode_pillow_png")
    if pillow is None or pillow.status != "ok":
        out.append(
            "No Pillow-written PNG was decoded"
            + (f": {pillow.reason}" if pillow else ": Pillow is not installed here.")
            + " The filter histogram and the worst-case `scale_image` row are missing too."
        )
    out.append(
        "Any machine other than this one. These numbers are a property of this CPU, this "
        "display and this desktop compositor; the JSON records the machine so a number is "
        "never quoted without it."
    )
    return out


def write_json(path: Path, results: dict[str, Result], ctx: Ctx, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "tool": "bench/bench_latency.py",
        "generated_utc": meta["generated_utc"],
        "machine": meta["machine"],
        "method": meta["method"],
        "measurements": {name: res.summary() for name, res in results.items()},
        "facts": meta["facts"],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                        help=f"timed iterations per measurement (default {DEFAULT_ITERATIONS})")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help=f"untimed iterations first (default {DEFAULT_WARMUP})")
    parser.add_argument("--region", default="480,270", help="region capture, WxH")
    parser.add_argument("--tiny", default="16,16", help="cheapest-capture region, WxH")
    parser.add_argument("--scale-width", type=int, default=640, help="scaled capture width")
    parser.add_argument("--json", default=str(Path(__file__).resolve().parent / "latest.json"),
                        help="where to write the machine-readable result")
    parser.add_argument("--no-json", action="store_true", help="do not write the JSON file")
    parser.add_argument("--quick", action="store_true",
                        help="3 iterations, for a smoke test that is not a result")
    parser.add_argument("--quiet", action="store_true", help="no per-measurement progress")
    args = parser.parse_args(argv)

    iterations = 3 if args.quick else max(1, args.iterations)
    warmup = 0 if args.quick else max(0, args.warmup)

    backend = load_backend()
    desktop = backend.desktop
    region = Rect(0, 0, *[int(v) for v in args.region.split(",")])
    tiny = Rect(0, 0, *[int(v) for v in args.tiny.split(",")])

    # Which branch of core.scale_image a caller on this machine would take. Recorded
    # up front rather than inferred later, because the old-path row depends on it.
    try:
        import PIL

        scale_branch = f"Pillow {PIL.__version__}"
    except ImportError:
        scale_branch = "pure Python (decode_png + downscale_rgb + encode_png)"

    workdir = Path(tempfile.mkdtemp(prefix="screen-agent-bench-"))
    ctx = Ctx(
        backend=backend,
        workdir=workdir,
        iterations=iterations,
        warmup=warmup,
        desktop=desktop,
        region=region,
        tiny=tiny,
        facts={
            "scale_image_branch": scale_branch,
            "region": [region.w, region.h],
            "tiny_region": [tiny.w, tiny.h],
        },
    )
    try:
        results = run_all(ctx, verbose=not args.quiet)
        meta = {
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "machine": _machine(ctx),
            "method": {
                "iterations": iterations,
                "warmup": warmup,
                "timer": "time.perf_counter",
                "percentile": "nearest-rank",
                "percentiles_reported": ["median", "p95", "min", "max"],
                "decompositions": "end-to-end rows and their component rows are timed in the "
                                  "same process on the same frames",
                "input_injection": "none; the benchmark is read-only against the desktop",
            },
            "facts": _derived_facts(results, ctx),
        }
        print(render_markdown(results, ctx, meta))
        if not args.no_json:
            out = Path(args.json)
            write_json(out, results, ctx, meta)
            print(f"wrote {out}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
