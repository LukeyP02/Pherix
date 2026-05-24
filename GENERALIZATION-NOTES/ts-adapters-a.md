# T1 — TS adapters batch A (git, s3, redis, mongodb, mysql)

Pressure log. Append-only. A7 harvests this into one coherent generalisation +
slim of both engines, mirrored Python↔TS.

### Async constructors cannot run the eager side-table DDL (MySQL)
- **Doing:** MySQLAdapter mirrors the Python adapter, which creates the
  `_pherix_versions` side-table *eagerly in `__init__`* so the first
  `read_version` on an unknown key returns 0 rather than a missing-table error.
- **Resisted:** the frozen protocol gives the adapter only a synchronous
  constructor, but the mysql2 driver's query API is async-only (no sync form,
  exactly the divergence `base.ts` already documents for the lane). You cannot
  `await` the DDL in a constructor, so the eager-create cannot be mirrored.
- **Smallest fix:** none needed in the engine. Worked around by an
  `ensureVersionsTable()` guard run once on the first async path
  (`readVersion`/`writeVersion`). Observably identical to a caller. Logged
  because it is the *second* adapter (after PostgresAdapter) to hit the
  sync-Python / async-TS construction-vs-lifecycle gap — if a third appears, the
  general form is an **optional async `init()` lifecycle hook** on the protocol
  (called once at txn begin / first use), which would let every async-driver
  adapter move eager constructor work onto a single awaited seam instead of each
  rolling its own lazy guard. Not worth adding for two; flagging the trend.

### apply() is synchronous in the protocol; async tools surface via the return value
- **Doing:** the s3/redis/mongodb adapters wrap async backend drivers, so their
  tools are `async` and `apply()` returns a promise; the partial-failure path
  needs a tool that fails mid-effect to propagate cleanly.
- **Resisted:** nothing — the protocol's `apply(effect, toolFn): unknown` is
  honest here. The runtime already wraps `adapter.apply` in
  `async () => adapter.apply(...)` and awaits it (runtime.ts:233/326), so a
  synchronous throw and a rejected promise propagate identically. A sync-tool
  test asserts with `expect(fn).toThrow`; an async-tool test with
  `rejects.toThrow`. No divergence, no engine change.
- **Smallest fix:** n/a — recording only to confirm the existing awaitable-lane
  generalisation in `base.ts` already covers async adapters' apply path. This is
  a *confirmation* of the abstraction, not pressure against it.
