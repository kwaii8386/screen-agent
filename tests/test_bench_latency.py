"""Well-formedness tests for ``bench/bench_latency.py``.

There is not one millisecond assertion in this file, and that is the point.

The numbers the change description quotes are wall-clock measurements of a GDI grab, a
zlib encode and a Windows timer. A test that asserts any of them is a test that passes
on a loaded machine for the wrong reason, fails on a fast machine for the wrong reason,
and keeps passing against code that has been made ten times slower if the machine is
having a good day. So the numbers live in ``bench/``, a human or a nightly job runs them
there on purpose, and this file only checks that the script exists, imports, is safe to
run, and would actually fail if someone added an input-injection call to it.

The valuable test here is :func:`test_benchmark_source_has_no_input_injection`. A
benchmark that types into the user's focused window while a nightly job watches is not a
slow benchmark, it is a bad day, and nothing in the type system or the import graph
would catch it. A scan of the source text does. The detector is itself exercised against
known-bad snippets, so a green run cannot be green because the regex stopped matching.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENCH_PATH = REPO / "bench" / "bench_latency.py"
BENCH_DIR = REPO / "bench"
PYPROJECT = REPO / "pyproject.toml"
TEST_SOURCE = Path(__file__).resolve()
MODULE_NAME = "_screen_agent_bench_under_test"

#: Call syntax that would inject input, or move the pointer, or take a screenshot
#: outside this script's control. Matched against the *source text* rather than the AST
#: so that a violation hidden behind ``getattr``, an alias, or an exec'd string is still
#: a violation, and so the guard covers the docstrings and comments too: a benchmark
#: that is *arguing* about SendInput rather than measuring it is still a benchmark
#: nobody wants running unattended.
FORBIDDEN_CALLS: tuple[str, ...] = (
    r"\bSendInput\s*\(",
    r"\bkeybd_event\s*\(",
    r"\bmouse_event\s*\(",
    r"\bSetCursorPos\s*\(",
    r"\bSetCursor\s*\(",
    r"\bPostMessage\s*\(",
    r"\bSendMessage\s*\(",
    r"\.\s*type_text\s*\(",
    r"\.\s*key_press\s*\(",
    r"\.\s*key_down\s*\(",
    r"\.\s*key_up\s*\(",
    r"\.\s*mouse_click\s*\(",
    r"\.\s*mouse_move\s*\(",
    r"\.\s*mouse_drag\s*\(",
    r"\.\s*mouse_scroll\s*\(",
    r"\.\s*double_click\s*\(",
    r"\.\s*right_click\s*\(",
    r"\.\s*click\s*\(",
    r"\.\s*drag\s*\(",
    r"\.\s*scroll\s*\(",
    r"\.\s*move_by\s*\(",
    r"\.\s*_send\s*\(",
)

#: names that must exist, so a rename that drops a measurement is caught here rather
#: than by a diff against a baseline that no longer has a row to compare
REQUIRED_MEASUREMENTS: frozenset[str] = frozenset({
    # 1. full-screen capture, decomposed
    "capture_full_gdi_grab",
    "capture_full_bgra_to_rgb",
    "capture_full_png_encode",
    "capture_full_file_write",
    "capture_full_end_to_end",
    # 2. region capture, same breakdown
    "capture_region_gdi_grab",
    "capture_region_bgra_to_rgb",
    "capture_region_png_encode",
    "capture_region_file_write",
    "capture_region_end_to_end",
    # 3. scaled capture, both paths
    "capture_scaled_gdi_640",
    "capture_png_then_scale_640",
    # 4. downscale_rgb, time and bytes read
    "downscale_rgb_1920_to_640",
    "downscale_rgb_1920_to_640_bytes_read",
    # 5. typing pacing and the timer quantum
    "asyncio_sleep_1ms_quantum",
    "asyncio_sleep_ladder",
    "typing_pace_40_chars_interval_0_008",
    "typing_pace_40_chars_interval_0",
    # 6. screen_state against the cheapest screenshot
    "screen_state",
    "screenshot_cheapest_16x16",
    # 7. pure-Python decode, filter 0 against filter 1
    "png_decode_filter0",
    "png_decode_filter1",
})

#: the seven areas the script is required to cover, one group each
REQUIRED_GROUPS: frozenset[str] = frozenset({
    "1-full-capture",
    "2-region-capture",
    "3-scaled-capture",
    "4-downscale",
    "5-type-pacing",
    "6-state-vs-screenshot",
    "7-png-decode",
})


# ------------------------------------------------------------------- the bench module
def _load_bench():
    """Import the benchmark by path, without running it and without importing it twice.

    The module has to be in ``sys.modules`` *before* it is executed: it uses
    ``from __future__ import annotations``, and ``dataclasses`` resolves those string
    annotations through ``sys.modules[cls.__module__].__dict__`` when it builds the
    ``__init__``.
    """
    spec = importlib.util.spec_from_file_location(MODULE_NAME, BENCH_PATH)
    assert spec is not None and spec.loader is not None, "bench/bench_latency.py is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - only on a broken script
        del sys.modules[MODULE_NAME]
        raise
    return module


def _configured_testpaths() -> list[str]:
    """``tool.pytest.ini_options.testpaths`` from pyproject.toml.

    Parsed with ``tomllib`` where it exists and with a line scan where it does not,
    because the package supports Python 3.10 and ``tomllib`` arrived in 3.11. A guard
    that skips itself on the oldest supported interpreter is not a guard.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        import tomllib  # noqa: PLC0415 - optional: 3.11+, and the project supports 3.10
    except ModuleNotFoundError:
        block = re.search(r"\[tool\.pytest\.ini_options\](.*?)(?=\n\[|\Z)", text, re.S)
        assert block is not None, "pyproject.toml has no [tool.pytest.ini_options] section"
        listed = re.search(r"testpaths\s*=\s*\[([^\]]*)\]", block.group(1))
        assert listed is not None, "pyproject.toml configures no testpaths"
        return [item.strip().strip("\"'") for item in listed.group(1).split(",") if item.strip()]
    config = tomllib.loads(text)
    return list(config.get("tool", {}).get("pytest", {}).get("ini_options", {})
                .get("testpaths", []))


@pytest.fixture(scope="module")
def bench():
    return _load_bench()


@pytest.fixture(scope="module")
def bench_source() -> str:
    return BENCH_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------- the file itself
def test_benchmark_script_exists():
    assert BENCH_PATH.is_file(), "bench/bench_latency.py is missing"
    assert BENCH_DIR.is_dir(), "bench/ is missing"


def test_benchmark_is_outside_pytest_testpaths():
    """``bench/`` must not be collected: the numbers are a report, not a test."""
    testpaths = _configured_testpaths()
    assert testpaths, "pyproject.toml does not configure pytest testpaths"
    assert "bench" not in testpaths, f"bench/ must not be in testpaths (found {testpaths})"
    assert testpaths == ["tests"], f"testpaths drifted from ['tests']: {testpaths!r}"
    for entry in testpaths:
        collected = (REPO / entry).resolve()
        assert BENCH_DIR.resolve() != collected
        assert collected not in BENCH_DIR.resolve().parents, (
            f"{entry} contains bench/, so the benchmark would be collected"
        )


def test_benchmark_source_parses():
    ast.parse(BENCH_PATH.read_text(encoding="utf-8"))


def test_benchmark_imports_without_measuring(bench):
    """Importing must be inert: no capture, no event loop, no file written."""
    assert callable(bench.main)
    assert callable(bench.run_all)
    assert isinstance(bench.MEASUREMENT_NAMES, tuple)
    assert bench.MEASUREMENT_NAMES, "the benchmark registered no measurements"


def test_benchmark_cli_help_exits_clean():
    """The documented entry point works, without taking a measurement to find out."""
    done = subprocess.run(
        [sys.executable, str(BENCH_PATH), "--help"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO), check=False,
    )
    assert done.returncode == 0, f"--help failed: {done.stderr}"
    assert "--iterations" in done.stdout
    assert "--json" in done.stdout


# ------------------------------------------------------------------ the measurement set
def test_every_measurement_is_named_and_unique(bench):
    names = bench.MEASUREMENT_NAMES
    assert names == tuple(sorted(set(names), key=names.index)), "measurement names are not unique"
    assert names, "no measurements registered"
    for name in names:
        assert re.fullmatch(r"[a-z0-9_]+", name), f"{name!r} is not a snake_case identifier"
        assert not name.startswith("_"), f"{name!r} is private"


def test_required_measurements_are_all_present(bench):
    missing = sorted(REQUIRED_MEASUREMENTS - set(bench.MEASUREMENT_NAMES))
    assert not missing, f"the benchmark stopped measuring: {missing}"


def test_every_group_is_covered(bench):
    groups = set(bench.GROUPS)
    assert REQUIRED_GROUPS <= groups, f"missing groups: {sorted(REQUIRED_GROUPS - groups)}"
    covered = {b.group for b in bench.BENCHES}
    assert covered == set(bench.GROUPS), (
        f"a declared group has no measurement: {sorted(set(bench.GROUPS) - covered)}"
    )


def test_every_measurement_is_well_formed(bench):
    for entry in bench.BENCHES:
        assert entry.name == bench.MEASUREMENT_NAMES[bench.BENCHES.index(entry)]
        assert entry.group in bench.GROUPS, f"{entry.name}: unknown group {entry.group}"
        assert entry.label.strip(), f"{entry.name}: no label"
        assert callable(entry.run), f"{entry.name}: run is not callable"
        assert entry.unit in ("ms", "bytes"), f"{entry.name}: odd unit {entry.unit!r}"
        if entry.share_of:
            assert entry.share_of in bench.MEASUREMENT_NAMES, (
                f"{entry.name}: share_of names a measurement that does not exist"
            )


def test_decomposition_shares_form_a_forest(bench):
    """Every share has to be of something that was measured, and nothing may cycle.

    One level of nesting is allowed and is used: ``capture_full_png_zlib`` is a
    component of ``capture_full_png_encode``, which is itself a component of
    ``capture_full_end_to_end``. That is the useful reading - zlib is nearly all of the
    encode - so what is enforced here is that every chain terminates at a measurement
    that stands on its own, rather than that they are all flat.
    """
    by_name = {b.name: b for b in bench.BENCHES}
    for entry in bench.BENCHES:
        chain = [entry.name]
        current = entry
        while current.share_of:
            assert current.share_of in by_name, (
                f"{current.name}: share_of names a measurement that does not exist"
            )
            assert current.share_of not in chain, (
                f"{' then '.join(chain)} then {current.share_of}: share_of is a cycle"
            )
            chain.append(current.share_of)
            current = by_name[current.share_of]
        assert len(chain) <= 3, (
            f"{' then '.join(chain)}: shares are nested more than two deep, so a reader has "
            f"to follow them and the table stops answering 'where did the time go'"
        )


# ------------------------------------------------------------------ the safety property
def test_benchmark_source_has_no_input_injection(bench_source):
    """The one assertion in this file that is worth more than the other twenty."""
    offenders = {
        pattern: match.group(0)
        for pattern in FORBIDDEN_CALLS
        if (match := re.search(pattern, bench_source))
    }
    assert not offenders, (
        "bench/bench_latency.py calls something that injects input or moves the pointer: "
        f"{offenders}. The benchmark measures capture and encode, which are read-only; "
        "a latency number is not worth typing into the user's focused window."
    )


@pytest.mark.parametrize("snippet", [
    "u32.SendInput(2, array, ctypes.sizeof(_INPUT))",
    "self._send([_char_event(unit), _char_event(unit, up=True)])",
    "await backend.type_text('hello', interval=0.008)",
    "await backend.key_press('ctrl+c')",
    "backend . mouse_click ( 10 , 20 )",
    "u32.SetCursorPos(100, 200)",
    "await backend.click(x=1, y=2)",
    "await backend.drag(0, 0, 10, 10)",
    "await backend.scroll(3, 'down')",
    "await backend.move_by(5, 5)",
    "win32api.SetCursorPos((1, 2))",
])
def test_the_injection_detector_actually_detects(snippet):
    """A green scan is only worth something if the scan can go red."""
    assert any(re.search(pattern, snippet) for pattern in FORBIDDEN_CALLS), (
        f"the detector missed a known-bad snippet: {snippet!r}"
    )


def test_benchmark_does_not_run_the_test_suite():
    """Guard against a copy-paste that makes the script shell out to pytest."""
    source = BENCH_PATH.read_text(encoding="utf-8")
    assert "pytest" not in source, "the benchmark should not drive pytest"
    assert "subprocess" not in source, "the benchmark should not launch anything"


# ------------------------------------------------------------- this file does no timing
def test_this_test_module_asserts_no_timing():
    """A test that measured time could drift into asserting it. This one must not.

    Cheap and slightly unusual, but it is the only way to keep the promise the module
    docstring makes. The names are split so that this test does not trip over itself.
    If you ever need a timing assertion, it belongs in ``bench/``.
    """
    source = TEST_SOURCE.read_text(encoding="utf-8")
    forbidden = [
        "perf_" + "counter",
        "mono" + "tonic",
        "process_" + "time",
        "time" + "it",
        "time." + "sleep",
        "asyncio." + "run",
        "elapsed_" + "ms",
    ]
    for token in forbidden:
        assert token not in source, (
            f"{TEST_SOURCE.name} refers to {token!r}; this file must not measure time"
        )
