"""Adversarial fuzzing at the policy + adapter boundary — fail loud / fail
closed, never silently wrong, never fail OPEN.

The kernel sits between an untrusted agent and production resources, so hostile
or malformed input must be either rejected loudly or absorbed without corrupting
state. The forbidden outcomes are two:

  - **silently wrong** — the world ends up in a half-applied / undefined state
    while the runtime reports success;
  - **fail OPEN** — a malformed / contradictory policy or cap config lets an
    effect through that a correct config would deny. For a guardrail, failing
    open is the *critical* failure: a denial that crashes is recoverable, a
    denial that silently passes is not.

We fuzz three boundaries, Hypothesis-driven where the space is large:

  A. malformed / oversized / non-serialisable tool args
  B. injection: SQL-ish payloads (parameterised SQL must render them inert),
     path-traversal (the FS adapter must stay rooted), control/unicode keys
  C. pathological policies & cap configs (contradictory allow+deny, zero /
     negative / nonexistent-tool caps, empty allow-lists) — must fail CLOSED.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.effects import Effect, EffectArgsError
from pherix.core.policy import Cap, Deny, Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool

from tests._laws import dump_kv, fresh_kv_conn

# Trust pillars: oversight (a pathological policy / cap config must fail CLOSED
# — never let a would-be-denied effect through) and blast radius (hostile or
# malformed input leaves the world untouched).
pytestmark = [pytest.mark.oversight, pytest.mark.blast_radius]

_FUZZ = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# === A. malformed / oversized / non-serialisable args ========================


@pytest.fixture
def kv_set_tool():
    @tool(resource="sql")
    def kv_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    return kv_set


# Values Pherix cannot deterministically journal — must raise EffectArgsError
# at Effect construction, BEFORE any snapshot/apply, leaving the world untouched.
_NON_SERIALISABLE = st.sampled_from(
    [
        object(),
        lambda: 1,
        {1, 2, 3},                 # set
        complex(1, 2),
        frozenset({1}),
        iter([1, 2, 3]),
        type,                      # a class object
    ]
)


@given(bad=_NON_SERIALISABLE)
@_FUZZ
def test_non_serialisable_arg_fails_loud_world_untouched(kv_set_tool, bad):
    """A non-journal-able arg is rejected at the idempotency boundary — loud
    (EffectArgsError) and before any state change. Silent str() coercion is
    the bug this forbids: two distinct objects must never collide on one
    effect_id, and nothing is applied."""
    conn = fresh_kv_conn()
    try:
        before = dump_kv(conn)
        with pytest.raises(EffectArgsError):
            with agent_txn({"sql": SQLiteAdapter(conn)}):
                kv_set_tool(k="x", v=bad)
        assert dump_kv(conn) == before
    finally:
        conn.close()


@pytest.fixture
def store_json_tool():
    @tool(resource="sql", name="store_json")
    def store_json(conn, k, blob):
        import json
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, json.dumps(blob, sort_keys=True)),
        )
        return blob

    return store_json


@given(
    payload=st.recursive(
        st.one_of(st.integers(), st.text(max_size=8), st.booleans(), st.none()),
        lambda children: st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=4), children, max_size=4),
        max_leaves=30,
    )
)
@_FUZZ
def test_deeply_nested_serialisable_args_round_trip(store_json_tool, payload):
    """Arbitrarily nested *serialisable* structures (lists/dicts of
    primitives) must journal and commit without crashing — they are valid,
    so they must be ACCEPTED, not spuriously rejected, and the world reflects
    them. (Pairs with the non-serialisable test: the boundary is type, not
    depth.)"""
    import json
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            store_json_tool(k="deep", blob=payload)
        stored = conn.execute("SELECT v FROM kv WHERE k='deep'").fetchone()[0]
        assert json.loads(stored) == payload
    finally:
        conn.close()


@given(none_key=st.sampled_from([None]))
@_FUZZ
def test_none_where_value_expected_is_handled(kv_set_tool, none_key):
    """None is serialisable, so passing it as a value is valid input and must
    commit cleanly (None where the tool's logic might not expect it is the
    tool's problem, not a kernel crash). The kernel must neither corrupt the
    journal nor fail-open silently — it journals None faithfully."""
    conn = fresh_kv_conn()
    try:
        # v=None violates the kv table's NOT NULL — the tool's INSERT raises,
        # which the runtime turns into a clean rollback (FAILED effect), NOT a
        # silent partial write.
        before = dump_kv(conn)
        with pytest.raises(sqlite3.IntegrityError):
            with agent_txn({"sql": SQLiteAdapter(conn)}):
                kv_set_tool(k="x", v=none_key)
        assert dump_kv(conn) == before  # rolled back — no partial state
    finally:
        conn.close()


@pytest.fixture
def put_blob_tool():
    @tool(resource="sql", name="put_blob")
    def put_blob(conn, k, big):
        conn.execute(
            "INSERT INTO blobs (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, big),
        )
        return len(big)

    return put_blob


@given(size_kb=st.integers(min_value=1, max_value=512))
@_FUZZ
def test_oversized_payload_round_trips_or_is_bounded(put_blob_tool, size_kb):
    """A large string value round-trips through commit + the journal without
    truncation or corruption — bounded behaviour, never a crash-to-undefined-
    state. We cap at 512 KiB per example to keep the suite fast; the 2 MiB case
    lives in test_laws_adversarial."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE blobs (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    big = "A" * (size_kb * 1024)
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            put_blob_tool(k="huge", big=big)
        stored = conn.execute("SELECT v FROM blobs WHERE k='huge'").fetchone()[0]
        assert stored == big  # lossless
    finally:
        conn.close()


@given(keylen=st.integers(min_value=1024, max_value=64 * 1024))
@_FUZZ
def test_oversized_key_round_trips(kv_inject_tool, keylen):
    """A very long KEY (not just value) round-trips — keys are data too."""
    conn = fresh_kv_conn()
    key = "K" * keylen
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            kv_inject_tool(k=key, v=1)
        assert dump_kv(conn) == {key: 1}
    finally:
        conn.close()


# === B. injection: SQL, path-traversal, control/unicode ======================


@pytest.fixture
def kv_inject_tool():
    @tool(resource="sql", name="kv_set_inj")
    def kv_set_inj(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    return kv_set_inj


_SQL_INJECTIONS = st.one_of(
    st.text(max_size=60),
    st.sampled_from(
        [
            "'; DROP TABLE kv; --",
            "x'); DELETE FROM kv; --",
            "1 OR 1=1",
            "'; CREATE TABLE evil(x); --",
            'robert"); DROP TABLE kv;--',
            "'; UPDATE kv SET v=0; --",
            "\x00; DROP TABLE kv",
            "'; ATTACH DATABASE '/tmp/x.db' AS x; --",
        ]
    ),
)


def _user_tables(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_pherix_%'"
        )
    }


@given(key=_SQL_INJECTIONS, val=_SQL_INJECTIONS)
@_FUZZ
def test_sql_injection_lands_as_data_never_executes(kv_inject_tool, key, val):
    """Parameterised SQL renders an injection inert: the payload lands as DATA,
    never executes. The schema is unchanged (no table dropped or created) and
    the key round-trips verbatim. This pins the parameterised-query claim — a
    real, load-bearing security property."""
    conn = fresh_kv_conn()
    try:
        tables_before = _user_tables(conn)
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            kv_inject_tool(k=key, v=1)
        # the schema is intact: injection executed nothing
        assert _user_tables(conn) == tables_before
        # the payload is stored as a literal string
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        assert row is not None and row[0] == 1
    finally:
        conn.close()


@given(
    key=st.sampled_from(["\x00embedded-nul", "pre\x00post", "\x00", "a\x00b\x00c"])
)
@_FUZZ
def test_embedded_nul_key_round_trips_without_truncation(kv_inject_tool, key):
    """The silently-wrong failure mode for an embedded NUL would be TRUNCATION
    at the NUL — that would collapse two distinct keys ('a\\x00b' and 'a') onto
    one, a real collision bug. Python's sqlite3 stores TEXT with embedded NULs
    verbatim, so the property to pin is: the key round-trips byte-for-byte
    (``WHERE k = ?`` finds it under the full NUL-bearing key), never truncated.
    If a future SQLite/driver started truncating, this test fails loud."""
    conn = fresh_kv_conn()
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            kv_inject_tool(k=key, v=1)
        # the full key (NUL and all) is the lookup key — no truncation
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        assert row is not None and row[0] == 1
        # and the stored key string equals the input exactly
        stored = conn.execute("SELECT k FROM kv").fetchone()[0]
        assert stored == key
    finally:
        conn.close()


_TRAVERSALS = st.one_of(
    st.lists(
        st.sampled_from(["..", "a", "b", "sub", "x.txt", ".", "etc", "passwd"]),
        min_size=1,
        max_size=6,
    ).map("/".join),
    st.sampled_from(
        [
            "../../etc/passwd",
            "../../../../../../etc/passwd",
            "/etc/passwd",
            "/absolute/outside",
            "sub/../../escape",
            "..",
            "....//....//etc/passwd",
        ]
    ),
)


@given(rel=_TRAVERSALS)
@_FUZZ
def test_fs_path_traversal_never_escapes_root(rel, tmp_path_factory):
    """A path-traversal string either resolves strictly inside root or is
    rejected loudly — it can NEVER write outside the root, however many ``..``
    segments or absolute prefixes it carries. Escaping the root is a real bug;
    if this ever fails, the FS adapter's containment is broken."""
    root = tmp_path_factory.mktemp("fsroot").resolve()
    adapter = FilesystemAdapter(root)
    adapter.begin()
    try:
        # A real backup dir so first-touch backup of an in-root file works.
        backup = adapter.backup_root / "e0"
        backup.mkdir()
        handle = FsHandle(root, backup, {})
        try:
            handle.write(rel, b"data")
        except (ValueError, OSError):
            return  # rejected loudly — nothing written outside root
        # If the write succeeded, the file MUST be inside root.
        written = (root / rel).resolve()
        assert written.is_relative_to(root), (
            f"path {rel!r} escaped root to {written}"
        )
    finally:
        adapter.rollback()


@given(rel=_TRAVERSALS)
@_FUZZ
def test_fs_version_lookup_never_escapes_root(rel, tmp_path_factory):
    """The version side-path (read_version) must apply the SAME containment as
    write — otherwise an isolation read could hash a file outside root. A
    traversal key either resolves in-root or raises ValueError."""
    root = tmp_path_factory.mktemp("fsroot2").resolve()
    adapter = FilesystemAdapter(root)
    try:
        adapter.read_version((rel,))
    except ValueError:
        return  # rejected as an escape attempt — good
    except OSError:
        # e.g. 'a/..' resolves to root itself → IsADirectoryError on hash.
        # That is an in-root path (containment held); the read just can't
        # hash a directory. Not an escape — acceptable.
        resolved = (root / rel).resolve()
        assert resolved.is_relative_to(root)
        return
    # If it returned a value, it only looked inside root (missing → sentinel).
    resolved = (root / rel).resolve()
    assert resolved.is_relative_to(root)


@given(
    key=st.text(
        alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF),
        min_size=1,
        max_size=30,
    )
)
@_FUZZ
def test_control_and_unicode_keys_round_trip_or_fail_loud(kv_inject_tool, key):
    """Control chars / arbitrary unicode (NUL, emoji, ...) in keys either
    round-trip verbatim OR fail loud — never silent corruption or truncation.
    A lone surrogate (e.g. '\\ud800') has no UTF-8 encoding, so SQLite raises
    UnicodeEncodeError and the txn rolls back cleanly; everything encodable is
    stored byte-for-byte. The forbidden middle ground is a key that stores
    *differently* from the input (silent mangling)."""
    conn = fresh_kv_conn()
    try:
        before = dump_kv(conn)
        try:
            with agent_txn({"sql": SQLiteAdapter(conn)}):
                kv_inject_tool(k=key, v=1)
        except (UnicodeEncodeError, ValueError):
            # loud rejection — and the world is untouched (clean rollback)
            assert dump_kv(conn) == before
            return
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        assert row is not None and row[0] == 1  # found under the exact key
        stored = conn.execute("SELECT k FROM kv").fetchone()[0]
        assert stored == key  # stored verbatim, not mangled
    finally:
        conn.close()


# === C. pathological policies & cap configs — must FAIL CLOSED ===============


@pytest.fixture
def write_tool():
    @tool(resource="sql", name="w")
    def w(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    return w


def _run(policy, write_tool, *, k="a", v=1):
    """Run one write under ``policy``; return ('ok'|exc_type_name, world)."""
    conn = fresh_kv_conn()
    try:
        try:
            with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
                write_tool(k=k, v=v)
            return "ok", dump_kv(conn)
        except PolicyViolation:
            return "denied", dump_kv(conn)
    finally:
        conn.close()


def test_contradictory_allow_and_deny_fails_closed(write_tool):
    """A tool that is BOTH allow-listed and deny-listed must be DENIED — deny
    wins. Failing open (letting it through because it's also allowed) would be
    the critical guardrail bug. And the world must be untouched on denial."""
    policy = Policy(allow={"w"}, deny={"w"})
    outcome, world = _run(policy, write_tool)
    assert outcome == "denied", "contradictory allow+deny FAILED OPEN"
    assert world == {}, "denied effect still mutated the world"


def test_empty_allowlist_denies_everything(write_tool):
    """An empty allow-list (allow=set()) is the most restrictive config: it
    permits NOTHING. A tool not in it must be denied — an empty allow-list that
    fails open (treats 'empty' as 'allow all') is a critical bug. Contrast with
    allow=None which means 'no allow-list, permit all'."""
    policy = Policy(allow=set())
    outcome, world = _run(policy, write_tool)
    assert outcome == "denied", "empty allow-list FAILED OPEN (allowed a tool)"
    assert world == {}


def test_allow_none_permits(write_tool):
    """The boundary check: allow=None means 'no allow-list' → permit. This is
    the other half of the empty-set semantics — if these two collapsed to the
    same behaviour the allow-list would be meaningless."""
    policy = Policy(allow=None)
    outcome, _ = _run(policy, write_tool)
    assert outcome == "ok"


@given(max_count=st.integers(min_value=-5, max_value=0))
@_FUZZ
def test_zero_or_negative_count_cap_denies_first_call(write_tool, max_count):
    """A count cap with max <= 0 admits ZERO calls — the first call must be
    denied (0 + 1 > max for any max <= 0). A cap that fails open on a
    nonsensical max is a critical bug. The world stays empty."""
    policy = Policy(caps=[Cap.count(tool="w", max=max_count)])
    outcome, world = _run(policy, write_tool)
    assert outcome == "denied", f"count cap max={max_count} FAILED OPEN"
    assert world == {}


@given(max_sum=st.integers(min_value=-1000, max_value=0))
@_FUZZ
def test_zero_or_negative_sum_cap_denies(write_tool, max_sum):
    """A sum cap with max <= 0 must deny any positive-contribution call (and a
    zero contribution against a negative max). Fail-closed under a nonsensical
    cap."""
    policy = Policy(caps=[Cap.sum(tool="w", via=lambda a: a["v"], max=max_sum)])
    outcome, world = _run(policy, write_tool, v=1)
    assert outcome == "denied", f"sum cap max={max_sum} FAILED OPEN"
    assert world == {}


def test_cap_on_nonexistent_tool_is_inert_not_crash(write_tool):
    """A cap targeting a tool that is never called must be a no-op — it neither
    crashes nor spuriously denies the tools that DO run. (applies_to() gates the
    cap on the tool name, so an unmatched cap contributes nothing.)"""
    policy = Policy(caps=[Cap.count(tool="does_not_exist", max=0)])
    outcome, world = _run(policy, write_tool)
    assert outcome == "ok"
    assert world == {"a": 1}


@given(
    via=st.sampled_from(
        [
            lambda a: a["missing_key"],   # KeyError inside the extractor
            lambda a: a["v"] / 0,          # ZeroDivisionError
            lambda a: object(),            # non-numeric contribution
        ]
    )
)
@_FUZZ
def test_pathological_sum_cap_extractor_does_not_fail_open(write_tool, via):
    """A sum cap whose ``via`` extractor raises or returns garbage must NOT let
    the effect through silently. The acceptable outcomes are: a loud error
    (the extractor raised) OR a denial — but NEVER a clean commit that bypassed
    the cap (fail-open). The world must not be silently mutated past a cap that
    couldn't evaluate."""
    policy = Policy(caps=[Cap.sum(tool="w", via=via, max=10)])
    conn = fresh_kv_conn()
    try:
        committed = False
        try:
            with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
                write_tool(k="a", v=5)
            committed = True
        except (PolicyViolation, KeyError, ZeroDivisionError, TypeError):
            pass  # loud OR denied — both acceptable
        if committed:
            # If it committed, the cap MUST have genuinely evaluated and allowed
            # — i.e. via returned a real number <= max. A garbage via that
            # silently let the write through is the fail-open bug.
            pytest.fail("write committed despite a non-evaluable sum cap (fail-open)")
        assert dump_kv(conn) == {}  # nothing partial-applied
    finally:
        conn.close()


def test_rule_that_raises_does_not_fail_open(write_tool):
    """A registered rule whose predicate itself raises must not let the effect
    through. The exception propagates (loud) and the txn rolls back — never a
    silent commit that bypassed a rule that couldn't decide."""
    policy = Policy.allow_all()

    @policy.rule
    def explode(effect, ctx):
        raise RuntimeError("rule predicate blew up")

    conn = fresh_kv_conn()
    try:
        with pytest.raises(RuntimeError):
            with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
                write_tool(k="a", v=1)
        assert dump_kv(conn) == {}  # rolled back, no partial state
    finally:
        conn.close()


@given(
    # A rule that denies based on a fuzzed predicate — proves denial is
    # deterministic and the world is always clean on deny, for any args.
    deny_when=st.integers(min_value=-100, max_value=100)
)
@_FUZZ
def test_rule_denial_is_deterministic_and_clean(write_tool, deny_when):
    """A rule that denies when v == deny_when must deny deterministically and
    leave the world empty; when it allows, the write lands. No middle ground."""
    policy = Policy.allow_all()

    @policy.rule
    def gate(effect, ctx):
        if effect.tool == "w" and effect.args.get("v") == deny_when:
            return Deny("blocked value")
        from pherix.core.policy import Allow
        return Allow()

    # denied case
    outcome, world = _run(policy, write_tool, v=deny_when)
    assert outcome == "denied"
    assert world == {}

    # allowed case (a different value)
    other = deny_when + 1
    outcome2, world2 = _run(policy, write_tool, v=other)
    assert outcome2 == "ok"
    assert world2 == {"a": other}
