"""Offline fake-MCP-client proof of the coding-CLI gateway's gate/rollback/journal path.

The Pherix coding gateway (``examples/coding_cli/gateway.py``) is the MCP server a
coding CLI (Aider / Claude Code / the reference client) spawns: every file / git /
shell tool the agent calls runs *through* Pherix as a journalled transaction. We do
not need a real CLI or a real MCP transport to prove the governance — we drive the
*same* gateway through the in-process MCP client, with a granted handshake identity,
against a real throwaway git repo, and assert the four-axes story end to end:

  * a reversible ``write_file`` **commits**, persists, and is journalled + attributed;
  * a broken ``apply_code_edit`` is written **live then rolled back** by Pherix — the
    file on disk is byte-identical to before, the effect is FAILED, the txn ROLLED_BACK
    (with a valid edit as the control, proving the rollback is the broken case, not a
    no-op refusal);
  * irreversible ``git_push`` / ``run_shell`` **gate** at commit (staged, never fired);
  * the obviously dangerous (force push, ``rm -rf`` of a protected path, secret writes,
    push via the reversible lane) is **denied at stage-time** (``policy_violation``),
    each paired with a benign control proving the rule is targeted, not a blanket ban;
  * an unrecognised identity hits the **deny-all floor**.

The gateway dispatches tools; it never calls a model — so this is fully offline, no
API key. GitAdapter shells out to real git, hence the real scratch repo per test.
"""

import importlib
import subprocess

import pytest

from pherix.core.tools import REGISTRY
from pherix.frontends.proxy import InProcessMCPClient, MCPClientError

from examples.coding_cli import CODING_CLI_IDENTITIES
from examples.coding_cli import gateway as gateway_mod

_TOOL_NAMES = (
    "read_file",
    "write_file",
    "delete_file",
    "apply_code_edit",
    "git_command",
    "git_push",
    "run_shell",
)

_GRANTED_IDENTITY = "pherix-coding-cli"


@pytest.fixture(autouse=True)
def _fresh_tools():
    # gateway.py registers its @tools at import time, and conftest's autouse
    # fixture clears the global REGISTRY before every test — so the decorators
    # have been wiped by the time a test body runs. Drop the names (defensive,
    # against a leak from another suite) then reload the module to re-run the
    # decorators into a clean registry. Torn down after so other suites start
    # clean. Use the reloaded module's build_gateway, never a stale top import.
    for name in _TOOL_NAMES:
        REGISTRY._tools.pop(name, None)
    importlib.reload(gateway_mod)
    yield
    for name in _TOOL_NAMES:
        REGISTRY._tools.pop(name, None)


@pytest.fixture
def repo(tmp_path):
    """A real throwaway git repo with one seeded, committed Python file.

    GitAdapter shells out to real git, so the gateway needs a genuine repo. We
    seed ``app.py`` with valid Python so apply_code_edit has a pre-edit state to
    restore to, and make the initial commit so git_command has a HEAD.
    """
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(root), check=True,
            capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.email", "test@pherix.local")
    git("config", "user.name", "Pherix Test")
    (root / "app.py").write_text("x = 1\n")
    git("add", "app.py")
    git("commit", "-q", "-m", "seed")
    return root


def _client(repo, identity=_GRANTED_IDENTITY):
    """Build the (reloaded) gateway rooted at ``repo`` and an initialised client."""
    gateway = gateway_mod.build_gateway(repo)
    client = InProcessMCPClient(gateway)
    client.initialize(identity=identity)
    return gateway, client


# -- 1. tools/list ---------------------------------------------------------


def test_tools_list_enumerates_all_seven_governed_tools(repo):
    _gateway, client = _client(repo)
    listed = {t["name"] for t in client.tool_descriptors()}
    assert set(_TOOL_NAMES) <= listed


# -- 2. reversible write commits + journals + attributed -------------------


def test_reversible_write_commits_journals_and_is_attributed(repo):
    gateway, client = _client(repo)
    structured = client.expect("write_file", {"path": "notes.txt", "content": "hello"})
    assert structured["committed"] is True

    # The write landed on disk through the real engine.
    assert (repo / "notes.txt").read_text() == "hello"

    # The effect is journalled APPLIED and the txn is attributed to the
    # handshake identity in the shared audit.
    txn_id = structured["txn_id"]
    effects = gateway.audit.get_effects(txn_id)
    assert [e["tool"] for e in effects] == ["write_file"]
    assert effects[0]["status"] == "APPLIED"
    txn = gateway.audit.get_transaction(txn_id)
    assert txn["client_id"] == _GRANTED_IDENTITY


# -- 3. THE ROLLBACK PROOF -------------------------------------------------


def test_broken_code_edit_is_written_live_then_rolled_back(repo):
    """The load-bearing test: Pherix writes the broken edit live, the compile
    check raises, and the reversible lane restores the pre-edit bytes — proving
    a *rollback of a live write*, not a mere pre-write refusal."""
    before = (repo / "app.py").read_text()
    assert before == "x = 1\n"

    gateway, client = _client(repo)
    envelope = client.call_tool("apply_code_edit", {"path": "app.py", "content": "x = ("})
    assert client.is_tool_error(envelope) is True
    structured = client.structured_of(envelope)
    # A SyntaxError in the tool body is a raised tool, not a policy denial.
    assert structured["pherix_error"] == "tool_raised"
    assert structured["committed"] is False

    # The file on disk is byte-identical to before: Pherix restored the live
    # write (it is NOT the broken content).
    assert (repo / "app.py").read_text() == before

    # The journal records the failed effect and the rolled-back transaction.
    txn_id = structured["txn_id"]
    effects = gateway.audit.get_effects(txn_id)
    assert [e["tool"] for e in effects] == ["apply_code_edit"]
    assert effects[0]["status"] == "FAILED"
    assert gateway.audit.get_transaction(txn_id)["state"] == "ROLLED_BACK"


def test_valid_code_edit_commits_and_persists(repo):
    """The control for the rollback proof: a *valid* edit commits and the new
    content persists — so the rollback above is demonstrably the broken case,
    not a no-op the gateway would do for any edit."""
    gateway, client = _client(repo)
    structured = client.expect(
        "apply_code_edit", {"path": "app.py", "content": "x = 2\n"}
    )
    assert structured["committed"] is True
    assert (repo / "app.py").read_text() == "x = 2\n"

    effects = gateway.audit.get_effects(structured["txn_id"])
    assert effects[0]["tool"] == "apply_code_edit"
    assert effects[0]["status"] == "APPLIED"


# -- 4. THE GATE PROOF -----------------------------------------------------


def test_git_push_gates_at_commit_and_never_fires(repo):
    """An irreversible push stages and gates: no compensator + no out-of-band
    approval in a one-shot MCP call, so commit() blocks before the push fires."""
    gateway, client = _client(repo)
    envelope = client.call_tool("git_push", {})
    assert client.is_tool_error(envelope) is True
    structured = client.structured_of(envelope)
    assert structured["pherix_error"] == "gate_blocked"
    assert structured["committed"] is False

    # The journal shows the push staged-and-GATED — recorded as intent, never
    # APPLIED. (No remote exists anyway; the point is it gated before firing.)
    effects = gateway.audit.get_effects(structured["txn_id"])
    assert [e["tool"] for e in effects] == ["git_push"]
    assert effects[0]["status"] == "GATED"


def test_benign_run_shell_gates_too(repo):
    """Irreversible-by-default: even a harmless shell command gates rather than
    fires — proving the staging is structural (the adapter says
    supports_rollback=False), not a per-command danger judgement."""
    gateway, client = _client(repo)
    envelope = client.call_tool("run_shell", {"command": "echo hello"})
    assert client.is_tool_error(envelope) is True
    structured = client.structured_of(envelope)
    assert structured["pherix_error"] == "gate_blocked"
    effects = gateway.audit.get_effects(structured["txn_id"])
    assert effects[0]["status"] == "GATED"


# -- 5. force push denied --------------------------------------------------


def test_force_push_is_denied_at_stage_time(repo):
    gateway, client = _client(repo)
    envelope = client.call_tool("git_push", {"force": True})
    assert client.is_tool_error(envelope) is True
    structured = client.structured_of(envelope)
    # A stage-time policy denial is stronger than the gate: nothing journalled
    # APPLIED, no resource touched.
    assert structured["pherix_error"] == "policy_violation"
    assert structured["committed"] is False


def test_force_push_via_extra_flag_is_denied(repo):
    # The `--force` spelling routed through `extra` is caught too.
    _gateway, client = _client(repo)
    envelope = client.call_tool("git_push", {"extra": "--force"})
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "policy_violation"


# -- 6. rm -rf of a protected path denied (targeted, not a shell ban) ------


@pytest.mark.parametrize("command", ["rm -rf /", "rm -rf .", "rm -rf ~"])
def test_destructive_rm_of_protected_path_is_denied(repo, command):
    _gateway, client = _client(repo)
    envelope = client.call_tool("run_shell", {"command": command})
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "policy_violation"


def test_non_destructive_shell_gates_not_policy_denied(repo):
    """The destructive-rm rule is targeted: a benign shell command is NOT
    policy-denied — it gates (irreversible lane). Proves the rule is not a
    blanket shell ban."""
    _gateway, client = _client(repo)
    envelope = client.call_tool("run_shell", {"command": "ls -la"})
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "gate_blocked"


# -- 7. secret writes denied (targeted, not a blanket write ban) -----------


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("write_file", {"path": ".env", "content": "API_KEY=sk-123"}),
        ("write_file", {"path": "secrets/key.txt", "content": "topsecret"}),
        ("apply_code_edit", {"path": "id_rsa", "content": "-----BEGIN KEY-----"}),
    ],
)
def test_secret_writes_are_denied(repo, tool_name, args):
    _gateway, client = _client(repo)
    envelope = client.call_tool(tool_name, args)
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "policy_violation"


def test_normal_source_write_is_allowed(repo):
    """The secret rule is targeted: an ordinary source write commits — proving
    it is not a blanket write ban."""
    gateway, client = _client(repo)
    structured = client.expect(
        "write_file", {"path": "src/foo.py", "content": "y = 2\n"}
    )
    assert structured["committed"] is True
    assert (repo / "src" / "foo.py").read_text() == "y = 2\n"


# -- 8. push via git_command denied (must use git_push) --------------------


def test_push_via_git_command_is_denied(repo):
    """A push routed through the *reversible* git_command lane would fire an
    irreversible network effect at stage-time, bypassing the gate — so it is
    denied; the agent must use the gated git_push tool."""
    _gateway, client = _client(repo)
    envelope = client.call_tool("git_command", {"command": "push origin main"})
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "policy_violation"


# -- 9. local git is reversible and commits --------------------------------


def test_local_git_command_commits_through_the_gateway(repo):
    """A local (reversible) git command commits through the gateway and is
    journalled APPLIED. The deep GitAdapter HEAD/stash restore is engine-tested
    elsewhere; here we only prove the reversible lane runs it live."""
    gateway, client = _client(repo)
    structured = client.expect(
        "git_command", {"command": "commit --allow-empty -m wip"}
    )
    assert structured["committed"] is True
    effects = gateway.audit.get_effects(structured["txn_id"])
    assert effects[0]["tool"] == "git_command"
    assert effects[0]["status"] == "APPLIED"


# -- 10. deny-all floor ----------------------------------------------------


def test_unknown_identity_hits_the_deny_all_floor(repo):
    """An identity the operator never granted runs under the deny-all default —
    even the otherwise-allowed write_file is refused, and nothing commits."""
    gateway = gateway_mod.build_gateway(repo)
    client = InProcessMCPClient(gateway)
    client.initialize(identity="rando")
    envelope = client.call_tool("write_file", {"path": "x.txt", "content": "nope"})
    assert client.is_tool_error(envelope) is True
    assert client.structured_of(envelope)["pherix_error"] == "policy_violation"
    assert not (repo / "x.txt").exists()
    # And expect() surfaces the same refusal as an exception.
    with pytest.raises(MCPClientError):
        client.expect("write_file", {"path": "y.txt", "content": "nope"})


# -- 11. run cap is present (not asserted to trip across calls) ------------


def test_run_shell_count_cap_is_present_in_policy():
    """The policy carries a per-txn count cap on run_shell.

    We assert the cap is PRESENT rather than asserting it trips across many
    tools/call invocations — and here is why that would be testing a falsehood:
    each MCP tools/call is its own one-shot transaction, and Cap.count bounds
    the number of times a tool fires *within a single txn*. A sequence of N
    separate run_shell calls is N separate txns of one effect each, so a per-txn
    cap of 8 never trips across them. The cap bounds a runaway *multi-effect*
    txn (e.g. a future batched/library-driven session), which is the model it
    is actually for. Asserting cross-call tripping would be asserting something
    false about the per-call MCP model.
    """
    policy = gateway_mod.coding_cli_policy()
    assert policy.caps, "expected at least one cap on the coding-CLI policy"
    run_shell_caps = [
        c for c in policy.caps if getattr(c, "tool", None) == "run_shell"
    ]
    assert run_shell_caps, "expected a count cap on run_shell"
    assert any(getattr(c, "max", None) == 8 for c in run_shell_caps)


# -- granted-identity coverage --------------------------------------------


@pytest.mark.parametrize("identity", CODING_CLI_IDENTITIES)
def test_every_granted_identity_can_commit_a_reversible_write(repo, identity):
    """Each handshake identity in CODING_CLI_IDENTITIES maps to the coding
    policy and can do ordinary reversible work."""
    _gateway, client = _client(repo, identity=identity)
    structured = client.expect(
        "write_file", {"path": f"by_{identity}.txt", "content": "ok"}
    )
    assert structured["committed"] is True
