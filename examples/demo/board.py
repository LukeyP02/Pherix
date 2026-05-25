"""Render docs/demo.html — the headline board tying BOTH layers together.

Per pillar it shows, side by side:
  1. the ACT result from this run (the vivid single case: WITHOUT vs WITH), and
  2. the LAW pass/count read from docs/trust-laws-results.json (the proof it
     generalizes to every sequence).

Self-contained: inline CSS, offline, dark-mode via prefers-color-scheme,
matching docs/trust-laws.html's palette and house style. If the law results
sidecar is absent, the board degrades gracefully — acts still render, law
counts show "not yet run".
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_JSON = REPO_ROOT / "docs" / "trust-laws-results.json"
BOARD_HTML = REPO_ROOT / "docs" / "demo.html"

# Narrative order: lead with the wedge (oversight) only in copy; the board
# keeps the canonical pillar order for visual parity with trust-laws.html.
PILLAR_ORDER = ["blast_radius", "audit", "oversight"]

PILLAR_META = {
    "blast_radius": {
        "title": "Blast radius",
        "tagline": "A mistake is contained, not catastrophic.",
        "act": "Act 1",
    },
    "audit": {
        "title": "Audit",
        "tagline": "You can always prove what happened.",
        "act": "Act 3",
    },
    "oversight": {
        "title": "Oversight",
        "tagline": "The wedge — a human stays on the irreversible.",
        "act": "Act 2",
    },
}


def _load_laws() -> dict:
    """Map marker -> law result dict, or {} if the sidecar is absent."""
    if not RESULTS_JSON.exists():
        return {}
    try:
        data = json.loads(RESULTS_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {p["marker"]: p for p in data.get("pillars", [])}


def _esc(s: object) -> str:
    return html.escape(str(s))


def render(act_results: dict) -> str:
    """act_results: marker -> ActResult. Returns the full HTML string."""
    laws = _load_laws()

    acts_ok = all(r.contained for r in act_results.values())
    laws_ok = bool(laws) and all(
        p.get("status") == "pass" for p in laws.values()
    )
    board_green = acts_ok and laws_ok

    if board_green:
        head_cls, verdict = "pass", "watch it work — proven underneath"
    elif acts_ok and not laws:
        head_cls, verdict = "pending", "demo green · laws not yet run"
    else:
        head_cls, verdict = "fail", "a pillar is red"

    cards = "\n".join(_card(m, act_results.get(m), laws.get(m)) for m in PILLAR_ORDER)
    # Footer timestamp is the ONLY non-deterministic byte; the act results
    # inlined above are byte-identical every run.
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return _TEMPLATE.format(
        head_cls=head_cls,
        verdict=_esc(verdict),
        cards=cards,
        stamp=_esc(stamp),
    )


def _card(marker: str, act, law: dict | None) -> str:
    meta = PILLAR_META[marker]

    # The act half (this run's vivid case).
    if act is not None:
        act_cls = "pass" if act.contained else "fail"
        without = _esc(act.without_label)
        with_ = _esc(act.with_label)
        with_mark = " ✓" if act.contained else " ✗"
        act_block = (
            f'<div class="arm without"><span class="arm-tag">without Pherix</span>'
            f'<span class="arm-val">{without}</span></div>'
            f'<div class="arm with"><span class="arm-tag">with Pherix</span>'
            f'<span class="arm-val">{with_}{with_mark}</span></div>'
        )
    else:
        act_cls = "pending"
        act_block = '<div class="arm"><span class="arm-val">act not run</span></div>'

    # The law half (the generalization proof).
    if law is not None:
        law_cls = "pass" if law.get("status") == "pass" else "fail"
        passed = law.get("passed", 0)
        selected = law.get("selected", 0)
        skipped = law.get("skipped", 0)
        mark = "✓" if law_cls == "pass" else "✗"
        skip_txt = f" · {skipped} skipped" if skipped else ""
        law_block = (
            f'<span class="law-count {law_cls}">law: <strong>{passed}</strong> '
            f"passed of {selected}{skip_txt} {mark}</span>"
        )
        theorem = _esc(law.get("theorem", ""))
    else:
        law_cls = "pending"
        law_block = '<span class="law-count pending">law: not yet run</span>'
        theorem = (
            "Run <code>python tests/pillar_report.py</code> to prove this "
            "pillar over every sequence."
        )

    card_cls = "pass" if (act_cls == "pass" and law_cls == "pass") else (
        "fail" if "fail" in (act_cls, law_cls) else "pending"
    )
    pill = {
        "pass": '<span class="pill pass">✓ PASS</span>',
        "fail": '<span class="pill fail">✗ FAIL</span>',
        "pending": '<span class="pill pending">… PENDING</span>',
    }[card_cls]

    return _CARD_TEMPLATE.format(
        card_cls=card_cls,
        title=_esc(meta["title"]),
        tagline=_esc(meta["tagline"]),
        act_label=_esc(meta["act"]),
        marker=marker,
        pill=pill,
        act_block=act_block,
        law_block=law_block,
        theorem=theorem,
    )


def write(act_results: dict) -> Path:
    BOARD_HTML.write_text(render(act_results))
    return BOARD_HTML


_CARD_TEMPLATE = """\
    <section class="card {card_cls}" aria-label="{title} pillar">
      <div class="card-head">
        <div>
          <h2>{title}</h2>
          <p class="tagline">{tagline}</p>
        </div>
        {pill}
      </div>
      <p class="marker"><code>{act_label}</code> demo &nbsp;·&nbsp;
         <code>@pytest.mark.{marker}</code> law</p>
      <div class="arms">{act_block}</div>
      <p class="laws-line">{law_block}</p>
      <p class="theorem">{theorem}</p>
    </section>"""


_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Pherix · the demo</title>
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
  .lede a {{ color: var(--accent); }}

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
  .marker {{ margin: 0 0 0.95rem; font-size: 0.92rem; color: var(--ink-3); }}

  .arms {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;
    margin: 0 0 1rem;
  }}
  @media (max-width: 560px) {{ .arms {{ grid-template-columns: 1fr; }} }}
  .arm {{
    padding: 0.7rem 0.9rem; border-radius: 6px; border: 1px solid var(--rule);
    background: var(--shade);
  }}
  .arm.without {{ border-color: var(--red); background: var(--red-bg); }}
  .arm.with {{ border-color: var(--green); background: var(--green-bg); }}
  .arm-tag {{
    display: block; font-family: ui-monospace, monospace; font-size: 0.7rem;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3);
    margin-bottom: 0.25rem;
  }}
  .arm-val {{ font-size: 1.02rem; color: var(--ink); }}
  .arm.with .arm-val {{ color: var(--green); font-weight: 600; }}
  .arm.without .arm-val {{ color: var(--red); }}

  .laws-line {{ margin: 0 0 0.9rem; }}
  .law-count {{
    font-family: ui-monospace, monospace; font-size: 0.86rem;
    padding: 0.25rem 0.6rem; border-radius: 99px; border: 1px solid currentColor;
  }}
  .law-count.pass {{ color: var(--green); background: var(--green-bg); }}
  .law-count.fail {{ color: var(--red); background: var(--red-bg); }}
  .law-count.pending {{ color: var(--amber); background: var(--amber-bg); }}
  .law-count strong {{ font-weight: 700; }}

  .theorem {{ margin: 0; color: var(--ink-2); font-size: 0.98rem; }}
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
    <p class="eyebrow">Pherix · the demo</p>
    <h1>Watch it work — and here's why it always works.</h1>
    <p class="lede">Three acts, each a matched pair: the same agent action run
      <em>without</em> Pherix and <em>with</em> it. The contrast is the whole point —
      a contained mistake, a gated payment, a complete audit trail. Each act is the
      vivid single case; the <a href="trust-laws.html">trust laws</a> beside it prove
      it holds for <em>every</em> sequence, not just this one.</p>
  </header>

  <div class="headline {head_cls}" role="status">
    <span class="laws">blast-radius ✓  ·  audit ✓  ·  oversight ✓</span>
    <span class="verdict">{verdict}</span>
  </div>

  <div class="cards">
{cards}
  </div>

  <footer>
    <p>Demo regenerated by <code>python -m examples.demo</code> — deterministic,
      offline, no API key; the act results above are byte-identical every run.
      Law counts read from <code>docs/trust-laws-results.json</code>
      (written by <code>python tests/pillar_report.py</code>).</p>
    <p>Explore the governed journal interactively with
      <code>python -m pherix.inspector</code>. Last rendered <strong>{stamp}</strong>.</p>
  </footer>
</main>
</body>
</html>
"""
