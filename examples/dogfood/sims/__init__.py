"""Tail-risk simulation suite — the unbiased proof of Pherix's value.

A careful agent does the job right *most* of the time. The value of a governance
layer is not "catch the agent every time" — it is catching the **rare**
catastrophic error (the 1-in-20, 1-in-50 moment) that, in a regulated domain
(insurance, finance, healthcare), is the one that costs millions. This package
measures exactly that: for each domain scenario it runs a real agent N times
ungoverned and N times governed, and compares the *natural disaster rate*
against the *residual rate with Pherix in the path*.

The whole suite is one command — ``bash sims.txt`` (see the repo root).
"""
