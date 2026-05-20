"""Examples package — demos and the real-agent dogfood suite.

Making ``examples`` a package lets the dogfoods run as modules
(``python -m examples.dogfood.<name>``) and lets the offline harness test
import the harness. Nothing here is part of the installable ``pherix``
wheel (see ``pyproject.toml`` ``[tool.hatch.build.targets.wheel]``).
"""
