/**
 * GitAdapter tests — snapshot/apply/restore against a real local git repo.
 * Mirror of tests/test_adapters_git.py.
 *
 * Exercises the adapter directly with synthesized Effects. A real `git` binary
 * is required; tests skip if absent. No network, no remote — this adapter
 * governs the LOCAL working tree only.
 */

import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Effect } from "../src/index.js";
import {
  GitAdapter,
  GitHandle,
  isTransactionalAdapter,
  shlexSplit,
} from "../src/adapters/index.js";

const HAS_GIT = spawnSync("git", ["--version"]).status === 0;
const d = HAS_GIT ? describe : describe.skip;

function git(root: string, ...args: string[]): string {
  const proc = spawnSync("git", args, { cwd: root, encoding: "utf8" });
  if (proc.status !== 0) throw new Error(`git ${args.join(" ")}: ${proc.stderr}`);
  return (proc.stdout ?? "").trim();
}

function effect(index = 0): Effect {
  return new Effect({
    txnId: "t",
    index,
    tool: "runGit",
    args: {},
    resource: "git",
    reversible: true,
  });
}

let repo: string;

beforeEach(() => {
  if (!HAS_GIT) return;
  repo = mkdtempSync(path.join(tmpdir(), "pherix_git_repo_"));
  git(repo, "init", "-q");
  git(repo, "config", "user.email", "t@example.com");
  git(repo, "config", "user.name", "t");
  writeFileSync(path.join(repo, "app.py"), "v1\n");
  git(repo, "add", "-A");
  git(repo, "commit", "-q", "-m", "c1");
  writeFileSync(path.join(repo, "app.py"), "v2\n");
  git(repo, "add", "-A");
  git(repo, "commit", "-q", "-m", "c2");
});

afterEach(() => {
  if (repo) rmSync(repo, { recursive: true, force: true });
});

d("GitAdapter", () => {
  it("satisfies the transactional protocol and is honestly reversible", () => {
    const a = new GitAdapter(repo);
    expect(isTransactionalAdapter(a)).toBe(true);
    expect(a.supportsRollback()).toBe(true);
    expect(a.name).toBe("git");
  });

  it("shlexSplit honours quoted commit messages", () => {
    expect(shlexSplit("commit -m 'work in progress'")).toEqual([
      "commit",
      "-m",
      "work in progress",
    ]);
    expect(shlexSplit('reset --hard "HEAD~2"')).toEqual(["reset", "--hard", "HEAD~2"]);
  });

  // --- the headline: a destroyed history is restored (left-inverse) ---------
  it("restores a hard-reset-destroyed history (restore ∘ apply ≈ identity)", () => {
    const headBefore = git(repo, "rev-parse", "HEAD");
    const logBefore = git(repo, "log", "--oneline");

    const a = new GitAdapter(repo);
    a.begin();
    try {
      const e = effect();
      e.snapshot = a.snapshot(e);
      // The agent nukes a commit off the history.
      a.apply(e, (h: GitHandle) => h.run("reset --hard HEAD~1"));
      expect(git(repo, "rev-parse", "HEAD")).not.toBe(headBefore); // damage happened
      expect(readFileSync(path.join(repo, "app.py"), "utf8")).toBe("v1\n");
      // Pherix folds the journal backward → the commit returns.
      a.restore(e.snapshot);
    } finally {
      a.rollback();
    }

    expect(git(repo, "rev-parse", "HEAD")).toBe(headBefore);
    expect(git(repo, "log", "--oneline")).toBe(logBefore);
    expect(readFileSync(path.join(repo, "app.py"), "utf8")).toBe("v2\n");
  });

  it("restores dirty tracked changes + untracked files", () => {
    writeFileSync(path.join(repo, "app.py"), "v2-wip\n");
    writeFileSync(path.join(repo, "notes.txt"), "scratch\n");

    const a = new GitAdapter(repo);
    a.begin();
    try {
      const e = effect();
      e.snapshot = a.snapshot(e);
      a.apply(e, (h: GitHandle) => h.run("reset --hard HEAD"));
      unlinkSync(path.join(repo, "notes.txt"));
      expect(readFileSync(path.join(repo, "app.py"), "utf8")).toBe("v2\n"); // wip lost
      a.restore(e.snapshot);
    } finally {
      a.rollback();
    }

    expect(readFileSync(path.join(repo, "app.py"), "utf8")).toBe("v2-wip\n");
    expect(readFileSync(path.join(repo, "notes.txt"), "utf8")).toBe("scratch\n");
  });

  it("removes an agent-created untracked file on restore", () => {
    const a = new GitAdapter(repo);
    a.begin();
    try {
      const e = effect();
      e.snapshot = a.snapshot(e);
      a.apply(e, (h: GitHandle) => h.run("status"));
      writeFileSync(path.join(repo, "leftover.tmp"), "junk\n"); // created after snapshot
      a.restore(e.snapshot);
    } finally {
      a.rollback();
    }
    expect(existsSync(path.join(repo, "leftover.tmp"))).toBe(false);
  });

  // --- partial failure: apply throws mid-effect, restore still lands at pre --
  it("partial failure: tool throws mid-effect, restore lands at pre-effect state", () => {
    const headBefore = git(repo, "rev-parse", "HEAD");
    writeFileSync(path.join(repo, "notes.txt"), "scratch\n");

    const a = new GitAdapter(repo);
    a.begin();
    try {
      const e = effect();
      e.snapshot = a.snapshot(e);
      expect(() =>
        a.apply(e, (h: GitHandle) => {
          h.run("reset --hard HEAD~1"); // damage lands
          throw new Error("boom mid-effect"); // ...then the tool fails
        }),
      ).toThrow("boom");
      // restore does not depend on apply completing.
      a.restore(e.snapshot);
    } finally {
      a.rollback();
    }

    expect(git(repo, "rev-parse", "HEAD")).toBe(headBefore);
    expect(readFileSync(path.join(repo, "notes.txt"), "utf8")).toBe("scratch\n");
  });

  it("GitHandle runs git rooted at the repo", () => {
    const out = new GitHandle(repo).run("rev-parse --abbrev-ref HEAD");
    expect(["main", "master"]).toContain(out);
  });
});
