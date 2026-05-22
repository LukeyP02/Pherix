"""The Pherix public site server.

A thin FastAPI app that (a) serves the static landing + governance pages in
``site/`` and (b) exposes the *real* engine over HTTP so the governance page's
policy preview runs ``pherix.governance.preview`` itself rather than a
JavaScript re-implementation. This package is a deploy artifact, not part of the
pip-installable library — the kernel (``pherix/core``) stays dependency-free; the
server just ``import pherix`` like any other consumer.
"""
