"""Runnable gateway entry point — ``python -m pherix.frontends.proxy <config>``.

A real MCP client (Claude Code, Cursor, an SDK) launches an MCP server as a
*subprocess* and talks newline-delimited JSON-RPC over its stdin/stdout. This
module is that subprocess: it builds a :class:`PherixGateway`, wraps it in a
:class:`MCPServer`, and runs :func:`serve_stdio` until stdin reaches EOF.

The config contract — why a Python factory, not a JSON file
------------------------------------------------------------
The gateway needs *live Python objects* that JSON cannot carry: resource
adapters bound to open connections, :class:`~pherix.core.policy.Policy`
instances (which hold rule *callables*), and the operator's registered
``@tool`` functions (the global ``REGISTRY`` is populated by import side-effect,
so the tool modules must actually be imported in this process). A static config
file can't express any of that. So ``<config>`` names a Python module or file
that exposes a single zero-argument factory::

    # my_gateway_config.py
    from pherix import SQLiteAdapter, Policy, AuditJournal, tool
    from pherix.frontends.proxy import PherixGateway
    import sqlite3

    @tool(resource="sql")
    def insert_user(conn, name):
        '''Insert a user row.'''
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    def build_gateway() -> PherixGateway:
        conn = sqlite3.connect("app.db", isolation_level=None)
        return PherixGateway(
            adapters={"sql": SQLiteAdapter(conn)},
            policies={"claude-code": Policy.allow_all()},
            default_policy=Policy(allow=set()),
            audit=AuditJournal("audit.db"),
        )

``build_gateway`` runs once at startup, in this process. Importing the config
module is what registers the ``@tool`` functions into the global ``REGISTRY`` —
so ``tools/list`` enumerates exactly the tools the factory's module defined.

``<config>`` is resolved two ways, tried in this order:

1. **Importable module path** — a dotted name like ``my_pkg.gateway_config``,
   resolved via :func:`importlib.import_module`. Use this when the config lives
   on ``sys.path`` (an installed package, or the cwd).
2. **Filesystem path** — a path to a ``.py`` file (``./configs/prod.py``),
   loaded via :func:`importlib.util.spec_from_file_location`. Use this for a
   standalone config file not on ``sys.path``.

A bare name containing no path separator and ending in neither ``.py`` nor a
slash is tried as a module first; anything that looks like a path (contains
``/`` or ``\``, or ends in ``.py``, or names an existing file) is loaded as a
file. If the module form fails to import and the string also names an existing
file, the file form is tried as a fallback — so both spellings of a cwd-local
``config.py`` work.

Shutdown is clean: :func:`serve_stdio` returns when stdin reaches EOF (the
client closed the pipe / the loop ended), and this module returns exit code 0.
A bad config (missing factory, import error, factory raised) exits non-zero with
a diagnostic on stderr *before* any JSON-RPC is served, so a launching client
sees the failure immediately rather than a silent dead pipe.

This module imports only the standard library and ``pherix`` — the library stays
dependency-free, and this entry point adds no new runtime dependency.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Any

from pherix.frontends.proxy.gateway import PherixGateway
from pherix.frontends.proxy.server import MCPServer
from pherix.frontends.proxy.transport import serve_stdio

_FACTORY = "build_gateway"


class ConfigError(Exception):
    """The config could not be loaded or did not expose a valid factory."""


def _looks_like_path(spec: str) -> bool:
    """True when ``spec`` is more naturally a filesystem path than a module name.

    A dotted module name (``my_pkg.config``) has no path separator and does not
    end in ``.py``; anything with a separator, a ``.py`` suffix, or that names an
    existing file is treated as a path. The ambiguous bare-name case
    (``config``) is handled by the caller, which tries the module form first and
    falls back to the file form.
    """
    return (
        os.sep in spec
        or (os.altsep is not None and os.altsep in spec)
        or spec.endswith(".py")
        or os.path.isfile(spec)
    )


def _load_module_by_name(name: str) -> Any:
    return importlib.import_module(name)


def _load_module_by_path(path: str) -> Any:
    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        raise ConfigError(f"config file does not exist: {path!r}")
    # A stable, collision-resistant module name keyed off the absolute path —
    # so two distinct config files do not clobber each other in sys.modules.
    mod_name = "_pherix_gateway_config_" + str(abs(hash(abspath)))
    spec = importlib.util.spec_from_file_location(mod_name, abspath)
    if spec is None or spec.loader is None:
        raise ConfigError(f"could not load config file: {path!r}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so a config that imports itself / uses __name__ works.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def load_config_module(spec: str) -> Any:
    """Resolve ``spec`` to a Python module (importable name or file path).

    Module form is tried first for a bare name; the file form is the fallback
    when the spec looks like a path or when the module import fails but the
    spec names an existing file. Raises :class:`ConfigError` with a single,
    actionable message on any failure.
    """
    if _looks_like_path(spec):
        return _load_module_by_path(spec)
    try:
        return _load_module_by_name(spec)
    except ImportError as exc:
        # A bare name that isn't importable but *is* a file on disk: fall back
        # to the file loader (handles ``python -m ... config.py``-style typos
        # and cwd-local files not on sys.path).
        if os.path.isfile(spec):
            return _load_module_by_path(spec)
        raise ConfigError(
            f"could not import config module {spec!r}: {exc}. Pass either an "
            f"importable dotted module name on sys.path, or a path to a .py "
            f"file exposing build_gateway()."
        ) from exc


def build_gateway_from_config(spec: str) -> PherixGateway:
    """Load the config module and invoke its ``build_gateway()`` factory.

    Importing the module is the side-effect that registers the operator's
    ``@tool`` functions into the global ``REGISTRY``. The factory must return a
    :class:`PherixGateway`; anything else is a config error.
    """
    module = load_config_module(spec)
    factory = getattr(module, _FACTORY, None)
    if factory is None or not callable(factory):
        raise ConfigError(
            f"config {spec!r} does not expose a callable {_FACTORY}() factory. "
            f"Define `def {_FACTORY}() -> PherixGateway:` in that module."
        )
    gateway = factory()
    if not isinstance(gateway, PherixGateway):
        raise ConfigError(
            f"{spec}.{_FACTORY}() returned {type(gateway).__name__}, expected a "
            f"PherixGateway."
        )
    return gateway


def main(argv: list[str] | None = None) -> int:
    """CLI entry: parse args, build the gateway, serve MCP over stdio.

    Returns the process exit code: 0 on a clean EOF shutdown, 2 on a usage /
    config error (reported on stderr before any JSON-RPC is served).
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        prog = "python -m pherix.frontends.proxy"
        print(
            f"usage: {prog} <config>\n\n"
            f"  <config>  importable module path (my_pkg.config) or path to a\n"
            f"            .py file, exposing build_gateway() -> PherixGateway.\n"
            f"            Importing it registers the operator's @tool functions.\n\n"
            f"Serves the Pherix MCP gateway over stdio (newline-delimited\n"
            f"JSON-RPC 2.0) until stdin EOF.",
            file=sys.stderr,
        )
        # No config given is a usage error; an explicit -h/--help is success.
        return 0 if args and args[0] in ("-h", "--help") else 2

    try:
        gateway = build_gateway_from_config(args[0])
    except ConfigError as exc:
        print(f"pherix gateway: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any factory fault is a startup error
        print(
            f"pherix gateway: build_gateway() raised "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    server = MCPServer(gateway)
    # serve_stdio returns when stdin reaches EOF — the client closed the pipe.
    # That is the clean-shutdown path; nothing else to tear down here (adapters
    # and the audit journal are the config's to own).
    serve_stdio(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
