# ts-adapters-b — pressure log (T2: dynamodb, gcs, elasticsearch, rest, messagequeue)

## Python twin depth audit
All five Python twins are at honest base depth, not thin stubs:

- `dynamodb.py`, `gcs.py`, `elasticsearch.py` — full snapshot/apply/restore
  (item / blob / document before-image capture + rewrite), reversible, real
  conformance batteries (`tests/test_adapters_{dynamodb,gcs,elasticsearch}.py`)
  with left-inverse, multi-key, partial-failure, deep-copy, custom-key tests.
  Mirrored 1:1 in TS.
- `rest.py`, `messagequeue.py` — HTTPAdapter-shaped irreversible adapters
  (`supports_rollback() -> False`, snapshot/restore raise) + a real harness
  (`rest_tool`/`graphql_tool`, `publish_tool`/`tombstone_compensator`) with
  end-to-end staged/gated/compensated tests. Mirrored 1:1.

No thinness to flag. One *intentional* divergence worth recording (not thinness):

### `read_version` / `write_version` exist on Python's irreversible adapters but not TS's
- **Doing:** mirroring `RESTAdapter` / `MQAdapter`.
- **Resisted:** the Python irreversible adapters define `read_version` /
  `write_version` that *raise* `IrreversibleAdapterError`, because Python's
  isolation layer probes for those methods. The TS isolation layer
  (`isolation.ts` `isVersionedAdapter`) instead *structurally detects* the
  methods' absence — so the TS `HttpAdapter` (the template) omits them entirely,
  and adding raising stubs would mis-signal "this adapter is versioned" to the
  structural check.
- **Smallest fix:** none needed — this is the TS structural-detection idiom
  working as designed. Recording it only so A7/T3 know the Py-has-raising-stubs /
  TS-omits asymmetry is deliberate and parity-safe (both correctly exclude
  irreversible effects from isolation diffing, by different means).

### Sync-vs-async `apply` and the partial-failure property (CONFIRMED cross-stream)
- **Doing:** writing the partial-failure test for the reversible store adapters
  (dynamodb/gcs/elasticsearch) — "tool mutates one key, then throws mid-effect;
  `restore` must still land every captured key."
- **Resisted:** nothing resisted *me* — but the shape is load-bearing and the
  same pressure surfaces in T1's redis/mongodb. The adapter `apply` contract is
  "sync or async, runtime awaits it" (`base.ts`). When the tool body is
  *synchronous* and throws, `apply` throws synchronously; when it is *async* and
  throws, `apply` returns a rejecting promise. My partial-failure tests use
  `async` tool callbacks, so `await adapter.apply(...)` rejects and
  `.rejects.toThrow` works. T1's redis/mongodb partial-failure tests use *sync*
  throwing callbacks against a sync `apply`, so the throw escapes synchronously
  and `.rejects.toThrow` does NOT catch it — those two tests fail at HEAD
  (T1-owned, pre-existing relative to my work; not the frozen engine, not my
  files).
- **Smallest fix:** this is fundamentally a *test-authoring* asymmetry, not an
  engine defect — the engine correctly awaits a value that may be sync or async.
  The general lesson for A7: the partial-failure conformance property should be
  pinned to the **async** tool shape (the normal TS case, and the one the
  runtime's `await` makes uniform), so adapter authors don't accidentally write
  a sync-throw test that bypasses the promise path. No substrate change — a
  conformance-harness convention. Flagging because it is the kind of divergent
  per-adapter test-shape A7's slim pass should normalise into one shared helper.
