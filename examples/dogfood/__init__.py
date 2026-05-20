"""Dogfood suite — real LLM agents with Pherix genuinely in the tool-call path.

The deterministic mechanism-proof lives in the 331-test suite. These dogfoods
are the opposite: a real model making real (sometimes wrong) decisions, with
Pherix catching what would hurt — an unwound release, a gated filesystem write,
an isolated concurrent ledger reconciliation.

The library (``pherix/``) stays dependency-free and never reads a key. Only this
package imports ``anthropic`` and reads ``ANTHROPIC_API_KEY`` — and only on the
real-run path, never in the offline test suite (tests inject a mock client).
See ``README.md``.
"""
