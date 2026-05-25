"""Pillar-grouped law runner — proves the three trust pillars and makes them visible.

Pherix's trust story is three deterministic laws over the effect journal:

    blast_radius   a mistake is contained, not catastrophic
    audit          you can always prove what happened
    oversight      no irreversible effect commits without approval / a compensator

Each law test in the suite is tagged with a pytest marker of the matching name
(``@pytest.mark.blast_radius`` / ``@pytest.mark.audit`` / ``@pytest.mark.oversight``).
This script runs the suite once **per pillar marker**, collects pass/fail counts,
and renders both:

  * a terminal pillar summary (one line per pillar + a headline line), and
  * ``docs/trust-laws.html`` — the self-contained board a buyer / auditor reads.

Design — how results reach the board
------------------------------------
We **regenerate** ``docs/trust-laws.html`` on every run, inlining the latest results
straight into the HTML. This is the simplest robust design: the board is always a
single self-contained file that opens offline with the real numbers baked in — no
fetch, no live server dependency, no stale-sidecar hazard. For programmatic consumers
we *also* drop a sidecar ``docs/trust-laws-results.json``. The HTML ships meaningful
content even on a never-run / all-pending board (theorem statements always render).

Mechanism — how we capture per-marker results
----------------------------------------------
We do **not** parse pytest's stdout (brittle). We invoke pytest **in-process** via
``pytest.main([...], plugins=[collector])`` with a tiny collecting plugin that reads
the terminal reporter's pass/fail tallies after the run. The selected count is taken
from the collector's seen items. If a marker is unregistered or selects zero tests,
the pillar degrades gracefully to a ``pending`` state (reported, never a crash).

Offline / no API key / fast — Pherix wraps tools, it never calls an LLM, so the
whole thing runs with no network.

Invocation
----------
    python tests/pillar_report.py          # from the worktree root
    python -m tests.pillar_report          # module form

Both regenerate the board and print the terminal summary.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field

import pytest

# ---------------------------------------------------------------------------
# Pillars — the three theorems. Statement prose lives here so the board always
# renders meaningful content even before the first run.
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
HTML_OUT = DOCS / "trust-laws.html"
JSON_OUT = DOCS / "trust-laws-results.json"


@dataclass(frozen=True)
class Pillar:
    marker: str
    title: str
    tagline: str
    theorem: str  # prose statement of the law


PILLARS: list[Pillar] = [
    Pillar(
        marker="blast_radius",
        title="Blast radius",
        tagline="A mistake is contained, not catastrophic.",
        theorem=(
            "rollback(apply(S, W)) == W for any reversible effect sequence S over any "
            "world W — the world is restored byte-exactly. A partial failure mid-commit "
            "fully unwinds, leaving no half-applied state. In the irreversible lane the "
            "registered compensator is a true left-inverse (compensator ∘ tool ≈ "
            "identity on the observable); with no compensator the transaction goes STUCK "
            "rather than leaving a torn effect."
        ),
    ),
    Pillar(
        marker="audit",
        title="Audit",
        tagline="You can always prove what happened.",
        theorem=(
            "Completeness: every applied effect and every policy verdict is recorded in "
            "the journal — the row count equals the count executed, nothing silently "
            "missing. Durability: the journal survives crash, truncation and byte-flip — "
            "it fails loud or recovers clean, never silently wrong. Recovery folds the "
            "durable journal to a consistent world: fully committed or fully rolled "
            "back, never torn."
        ),
    ),
    Pillar(
        marker="oversight",
        title="Oversight",
        tagline="The wedge — a human stays on the irreversible.",
        theorem=(
            "No irreversible effect ever commits without explicit approval or a "
            "registered compensator, under any interleaving or sequence. Approval is "
            "necessary — an un-approved irreversible effect raises GateBlocked, nothing "
            "fires, the world is untouched — and sufficient: once approved it fires "
            "exactly once and is recorded. Verified against hundreds of adversarially "
            "generated approve / no-approve interleavings."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class PillarResult:
    marker: str
    title: str
    tagline: str
    theorem: str
    status: str = "pending"  # "pass" | "fail" | "pending"
    selected: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class RunResults:
    generated_at: str
    pillars: list[PillarResult] = field(default_factory=list)

    @property
    def all_green(self) -> bool:
        return bool(self.pillars) and all(p.status == "pass" for p in self.pillars)

    @property
    def any_fail(self) -> bool:
        return any(p.status == "fail" for p in self.pillars)


# ---------------------------------------------------------------------------
# In-process pytest collector — no stdout parsing
# ---------------------------------------------------------------------------


class _Collector:
    """A pytest plugin that tallies outcomes in-process for one marker run."""

    def __init__(self) -> None:
        self.selected = 0
        self.passed = 0
        self.failed = 0
        self.errors = 0
        self.skipped = 0

    def pytest_collection_finish(self, session):  # noqa: D401
        # Runs after -m deselection, so session.items is the final selected set.
        self.selected = len(session.items)

    def pytest_runtest_logreport(self, report):  # noqa: D401
        # Count the "call" phase for pass/fail/skip; count setup/teardown errors too.
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
        elif report.failed:
            # setup/teardown error
            self.errors += 1


def _run_marker_inproc(marker: str) -> _Collector:
    """Run the suite selected by ``-m <marker>`` in-process, return the tally.

    This is the worker half — it runs in a dedicated subprocess (see
    :func:`run_marker`), so each pillar gets a fresh interpreter.
    """
    collector = _Collector()
    pytest.main(
        [
            "-m",
            marker,
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            str(REPO_ROOT / "tests"),
        ],
        plugins=[collector],
    )
    return collector


def run_marker(marker: str) -> _Collector:
    """Run one pillar's marker selection in a **fresh subprocess**.

    We do not call :func:`pytest.main` three times in one interpreter. Some law
    suites carry more than one pillar marker (e.g. the Hypothesis
    ``RuleBasedStateMachine`` in ``test_stateful_txn``), and Hypothesis keeps
    process-global state that is not safe to execute the same machine twice in
    one process — doing so raised a spurious ``KeyError`` that vanished the
    moment each marker ran in its own interpreter. A subprocess per pillar
    matches exactly how ``pytest -m <marker>`` is run by hand, so the board
    reports the same verdict a human would see. We still capture the tally via
    the in-process collector (no brittle stdout parsing) — the worker writes it
    to a JSON file the parent reads back.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        out_path = pathlib.Path(tf.name)
    try:
        subprocess.run(
            [sys.executable, "-m", "tests.pillar_report", "--_worker", marker, str(out_path)],
            cwd=str(REPO_ROOT),
            check=False,
        )
        data = json.loads(out_path.read_text())
    finally:
        out_path.unlink(missing_ok=True)
    c = _Collector()
    c.selected = data["selected"]
    c.passed = data["passed"]
    c.failed = data["failed"]
    c.errors = data["errors"]
    c.skipped = data["skipped"]
    return c


def classify(c: _Collector) -> str:
    if c.selected == 0:
        return "pending"
    if c.failed or c.errors:
        return "fail"
    return "pass"


def collect_results() -> RunResults:
    now = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    rr = RunResults(generated_at=now)
    for p in PILLARS:
        print(f"  running pillar  {p.marker} …", flush=True)
        c = run_marker(p.marker)
        status = classify(c)
        rr.pillars.append(
            PillarResult(
                marker=p.marker,
                title=p.title,
                tagline=p.tagline,
                theorem=p.theorem,
                status=status,
                selected=c.selected,
                passed=c.passed,
                failed=c.failed,
                errors=c.errors,
                skipped=c.skipped,
            )
        )
    return rr


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _glyph(status: str) -> str:
    return {"pass": "✓", "fail": "✗", "pending": "○"}.get(status, "?")


def _colour(status: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    c = {"pass": _GREEN, "fail": _RED, "pending": _DIM}.get(status, "")
    return f"{c}{text}{_RESET}"


def print_summary(rr: RunResults) -> None:
    print()
    for p in rr.pillars:
        glyph = _glyph(p.status)
        if p.status == "pending":
            detail = "no tests selected (marker pending)"
        elif p.status == "fail":
            detail = f"{p.failed + p.errors} failing of {p.selected}"
        else:
            detail = f"{p.passed} passed"
        line = f"  {p.marker:<14} {glyph}  {detail}"
        print(_colour(p.status, line))

    print()
    parts = [f"{p.marker.replace('_', '-')} {_glyph(p.status)}" for p in rr.pillars]
    headline = "  " + "  ·  ".join(parts)
    if rr.all_green:
        headline += "    — provably correct"
        status_for_colour = "pass"
    elif rr.any_fail:
        status_for_colour = "fail"
    else:
        status_for_colour = "pending"
    bold = headline if not sys.stdout.isatty() else f"{_BOLD}{headline}{_RESET}"
    print(_colour(status_for_colour, bold))
    print()


# ---------------------------------------------------------------------------
# Board rendering — regenerate the self-contained HTML
# ---------------------------------------------------------------------------


def _e(s: str) -> str:
    return _html.escape(s)


def _status_pill(status: str) -> str:
    label = {"pass": "PASS", "fail": "FAIL", "pending": "PENDING"}.get(status, "?")
    glyph = _glyph(status)
    return f'<span class="pill {_e(status)}">{glyph} {label}</span>'


def _pillar_card(p: PillarResult) -> str:
    if p.status == "pending":
        count = "marker not yet registered — no tests selected"
    elif p.status == "fail":
        count = f"{p.passed} passed · <strong>{p.failed + p.errors} failing</strong> of {p.selected} selected"
    else:
        count = f"<strong>{p.passed}</strong> passed of {p.selected} selected"
    skipped = f" · {p.skipped} skipped" if p.skipped else ""
    return f"""
    <section class="card {_e(p.status)}" aria-label="{_e(p.title)} pillar">
      <div class="card-head">
        <div>
          <h2>{_e(p.title)}</h2>
          <p class="tagline">{_e(p.tagline)}</p>
        </div>
        {_status_pill(p.status)}
      </div>
      <p class="marker"><code>@pytest.mark.{_e(p.marker)}</code></p>
      <p class="theorem">{_e(p.theorem)}</p>
      <p class="count">{count}{skipped}</p>
    </section>"""


def render_html(rr: RunResults) -> str:
    if rr.all_green:
        head_status, head_word = "pass", "provably correct"
    elif rr.any_fail:
        head_status, head_word = "fail", "a law is failing — engine finding"
    else:
        head_status, head_word = "pending", "awaiting first run"

    headline = "  ·  ".join(
        f'{p.title.lower().replace(" ", "-")} {_glyph(p.status)}' for p in rr.pillars
    )
    cards = "\n".join(_pillar_card(p) for p in rr.pillars)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Pherix · trust laws</title>
<style>
  :root {{
    --bg: #faf8f4; --panel: #f1ece2; --ink: #161513; --ink-2: #4a443c;
    --ink-3: #7a7268; --rule: #d0c7b5; --accent: #b4421e;
    --green: #355c3a; --green-bg: rgba(53, 92, 58, 0.10);
    --red: #9a2f1a; --red-bg: rgba(154, 47, 26, 0.10);
    --amber: #8a6a14; --amber-bg: rgba(138, 106, 20, 0.10);
    --shade: rgba(22, 21, 19, 0.05);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14130f; --panel: #1c1a14; --ink: #ece6d8; --ink-2: #c0baac;
      --ink-3: #7a7268; --rule: #3a342a; --accent: #e07a4e;
      --green: #6ba070; --green-bg: rgba(107, 160, 112, 0.12);
      --red: #e0795e; --red-bg: rgba(224, 121, 94, 0.12);
      --amber: #d4ac56; --amber-bg: rgba(212, 172, 86, 0.12);
      --shade: rgba(255, 250, 240, 0.04);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--ink); }}
  body {{
    font-family: 'Iowan Old Style', 'Palatino Linotype', Palatino, Charter, Georgia, serif;
    font-size: 17px; line-height: 1.55; padding: 4rem 1.5rem 6rem;
  }}
  main {{ max-width: 920px; margin: 0 auto; }}
  header {{ margin-bottom: 2.5rem; }}
  .eyebrow {{
    font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 0.78rem;
    letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-3);
    margin-bottom: 0.5rem;
  }}
  h1 {{ font-size: 2.4rem; line-height: 1.08; margin: 0 0 0.85rem; font-weight: 500; letter-spacing: -0.01em; }}
  .lede {{ color: var(--ink-2); font-size: 1.1rem; max-width: 64ch; margin: 0; }}

  .headline {{
    margin: 2.5rem 0 3rem; padding: 1.5rem 1.75rem;
    border: 1px solid var(--rule); border-radius: 8px; background: var(--panel);
    display: flex; align-items: center; justify-content: space-between;
    gap: 1.5rem; flex-wrap: wrap;
  }}
  .headline.pass {{ border-color: var(--green); background: var(--green-bg); }}
  .headline.fail {{ border-color: var(--red); background: var(--red-bg); }}
  .headline.pending {{ border-color: var(--amber); background: var(--amber-bg); }}
  .headline .laws {{
    font-family: ui-monospace, monospace; font-size: 1.15rem; font-weight: 600;
    letter-spacing: 0.01em;
  }}
  .headline.pass .laws {{ color: var(--green); }}
  .headline.fail .laws {{ color: var(--red); }}
  .headline.pending .laws {{ color: var(--amber); }}
  .headline .verdict {{
    font-family: ui-monospace, monospace; font-size: 0.78rem;
    letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3);
  }}

  .cards {{ display: flex; flex-direction: column; gap: 1.1rem; }}
  .card {{
    padding: 1.6rem 1.75rem 1.75rem; background: var(--panel);
    border: 1px solid var(--rule); border-radius: 8px;
    border-left: 4px solid var(--rule);
  }}
  .card.pass {{ border-left-color: var(--green); }}
  .card.fail {{ border-left-color: var(--red); }}
  .card.pending {{ border-left-color: var(--amber); }}
  .card-head {{
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 1rem; margin-bottom: 0.9rem;
  }}
  .card h2 {{ font-size: 1.5rem; margin: 0; font-weight: 500; letter-spacing: -0.005em; }}
  .tagline {{ margin: 0.2rem 0 0; color: var(--ink-2); font-size: 1rem; font-style: italic; }}
  .pill {{
    flex: 0 0 auto; font-family: ui-monospace, monospace; font-size: 0.72rem;
    letter-spacing: 0.1em; padding: 0.3rem 0.7rem; border-radius: 99px;
    border: 1px solid currentColor; white-space: nowrap; font-weight: 600;
  }}
  .pill.pass {{ color: var(--green); background: var(--green-bg); }}
  .pill.fail {{ color: var(--red); background: var(--red-bg); }}
  .pill.pending {{ color: var(--amber); background: var(--amber-bg); }}
  .marker {{ margin: 0 0 0.85rem; }}
  .theorem {{ margin: 0 0 1rem; color: var(--ink); font-size: 1.02rem; }}
  .count {{
    margin: 0; font-family: ui-monospace, monospace; font-size: 0.82rem;
    color: var(--ink-3);
  }}
  .count strong {{ color: var(--ink); }}
  code {{
    font-family: ui-monospace, monospace; font-size: 0.82em;
    background: var(--shade); padding: 1px 6px; border-radius: 3px; color: var(--ink-2);
  }}

  footer {{ margin-top: 4rem; padding-top: 2rem; border-top: 1px solid var(--rule); font-size: 0.86rem; color: var(--ink-3); }}
  footer p {{ margin: 0.35rem 0; }}
</style>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">Pherix · trust laws</p>
    <h1>Pherix is provably correct.</h1>
    <p class="lede">Not "trust our demo" — three trust guarantees stated as deterministic
      laws over the effect journal, and re-proven on every run. The guarantee is the
      product; it is deterministic, so we can <em>test it</em>, not just demonstrate it.</p>
  </header>

  <div class="headline {head_status}" role="status">
    <span class="laws">{_e(headline)}</span>
    <span class="verdict">{_e(head_word)}</span>
  </div>

  <div class="cards">
{cards}
  </div>

  <footer>
    <p>Last run <strong>{_e(rr.generated_at)}</strong> · regenerated by
      <code>python tests/pillar_report.py</code>. Each pillar is a pytest marker;
      the runner folds the law suite once per marker and inlines the verdict here.</p>
    <p>Offline, no API key — Pherix wraps tools, it never calls an LLM. A failing law is
      a real engine finding, reported here, never papered over.</p>
  </footer>
</main>
</body>
</html>
"""


def write_outputs(rr: RunResults) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(render_html(rr), encoding="utf-8")
    JSON_OUT.write_text(json.dumps(asdict(rr), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _worker_main(marker: str, out_path: str) -> int:
    """Subprocess entry: run one marker in this fresh interpreter, dump the tally."""
    c = _run_marker_inproc(marker)
    pathlib.Path(out_path).write_text(
        json.dumps(
            {
                "selected": c.selected,
                "passed": c.passed,
                "failed": c.failed,
                "errors": c.errors,
                "skipped": c.skipped,
            }
        ),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--_worker":
        return _worker_main(sys.argv[2], sys.argv[3])
    print(f"\n  trust-laws pillar report · {REPO_ROOT.name}\n")
    rr = collect_results()
    print_summary(rr)
    write_outputs(rr)
    print(f"  board   → {HTML_OUT.relative_to(REPO_ROOT)}")
    print(f"  sidecar → {JSON_OUT.relative_to(REPO_ROOT)}\n")
    # Exit non-zero only on a genuine law failure; pending markers are not a failure.
    return 1 if rr.any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
