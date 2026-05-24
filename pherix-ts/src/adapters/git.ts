/**
 * GitAdapter — snapshot/restore a real local git working tree.
 * Mirror of pherix/core/adapters/git.py.
 *
 * Git is the resource a coding agent touches most, and *locally* it is natively
 * reversible — which makes it a clean fit for the snapshot -> apply -> restore
 * protocol against machinery unlike SQL savepoints or filesystem copies:
 *
 *   - `snapshot` records the current `HEAD` commit, a stash object capturing the
 *     dirty *tracked* state (`git stash create`), and a copy of the *untracked*
 *     files (git's stash does not include them);
 *   - `apply` runs the agent's git operation (commit, branch, merge, rebase, or
 *     a destructive `reset --hard` / `checkout`);
 *   - `restore` reverts with `git reset --hard <head>` + `git clean` + a
 *     re-apply of the dirty stash + restoration of the backed-up untracked files.
 *
 * Because git keeps unreachable commits in the reflog, even a history the agent
 * *destroyed* (`reset --hard HEAD~5`) is recoverable — `reset --hard` back to
 * the recorded SHA brings every commit back.
 *
 * Honest boundary. Pushing to a remote leaves the machine and cannot be cleanly
 * un-pushed, so it is NOT reversible. This adapter governs the LOCAL repository
 * only; a `git push` belongs on the irreversible / commit-gate lane (a separate
 * resource). `supportsRollback()` is therefore `true` for everything this
 * adapter handles — the push boundary is enforced elsewhere, not pretended here.
 */

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import type { Effect, SnapshotHandle } from "../effects.js";
import type { ToolFn, TransactionalResourceAdapter } from "./base.js";

/**
 * Sentinel for readVersion on a repo with no commits yet (mirrors the
 * filesystem adapter's FS_MISSING — a non-null marker so isolation can tell
 * "read as empty" apart from a real SHA via a plain !== comparison).
 */
const GIT_EMPTY = "__no_head__";

/** A git subprocess exited non-zero. Carries the stderr for the journal. */
export class GitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GitError";
  }
}

/** Split a command string into argv, shell-safe (no shell is spawned). Mirrors
 *  Python's shlex.split for the common cases an agent writes — whitespace
 *  separation with single/double-quoted runs honoured. */
export function shlexSplit(command: string): string[] {
  const out: string[] = [];
  let cur = "";
  let inSingle = false;
  let inDouble = false;
  let sawToken = false;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i] as string;
    if (inSingle) {
      if (ch === "'") inSingle = false;
      else cur += ch;
      continue;
    }
    if (inDouble) {
      if (ch === '"') inDouble = false;
      else if (ch === "\\" && i + 1 < command.length) {
        const next = command[i + 1] as string;
        if (next === '"' || next === "\\") {
          cur += next;
          i++;
        } else cur += ch;
      } else cur += ch;
      continue;
    }
    if (ch === "'") {
      inSingle = true;
      sawToken = true;
      continue;
    }
    if (ch === '"') {
      inDouble = true;
      sawToken = true;
      continue;
    }
    if (ch === " " || ch === "\t" || ch === "\n") {
      if (sawToken) {
        out.push(cur);
        cur = "";
        sawToken = false;
      }
      continue;
    }
    cur += ch;
    sawToken = true;
  }
  if (inSingle || inDouble) throw new Error(`unbalanced quotes in command: ${command}`);
  if (sawToken) out.push(cur);
  return out;
}

/** Run a git command rooted at `repoRoot`; return stripped stdout. No shell is
 *  spawned (argv form), so the agent-supplied command cannot inject shell
 *  syntax. On non-zero exit with `check`, raises GitError carrying stderr so
 *  the failure is legible in the journal / to the model. */
function git(repoRoot: string, args: string[], check = true): string {
  const proc = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (proc.error) {
    throw new GitError(`git ${args.join(" ")} failed to spawn: ${proc.error.message}`);
  }
  if (check && proc.status !== 0) {
    throw new GitError(
      `git ${args.join(" ")} failed (${proc.status}): ${(proc.stderr ?? "").trim()}`,
    );
  }
  return (proc.stdout ?? "").trim();
}

/**
 * The per-effect git handle injected as the first arg of git tools.
 *
 * Exposes `run` — execute one git command, rooted at the repo — which is the
 * surface an agent's `runGit` tool calls. The command is the string a model
 * naturally writes (e.g. `"reset --hard HEAD~2"`, `"commit -m 'wip'"`); it is
 * split with `shlexSplit` (shell-safe — no shell is invoked) and passed to
 * `git` as an argv list, so there is no shell-injection surface.
 */
export class GitHandle {
  constructor(private readonly root: string) {}

  /** Run `git <command>` in the repo; return stdout (throws on failure). */
  run(command: string): string {
    return git(this.root, shlexSplit(command));
  }
}

interface GitPayload {
  head: string | null;
  stash: string;
  backupDir: string;
  untracked: string[];
}

export class GitAdapter implements TransactionalResourceAdapter {
  readonly name = "git";
  private readonly rootPath: string;
  private backupRoot: string | null = null;

  constructor(repoRoot: string) {
    this.rootPath = realpathSync(path.resolve(repoRoot));
  }

  get root(): string {
    return this.rootPath;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- transaction-scope lifecycle ----------------------------------------

  begin(): void {
    // A per-txn scratch dir for the untracked-file backups (git's stash
    // captures tracked changes only, so untracked files are copied aside).
    this.backupRoot = mkdtempSync(path.join(tmpdir(), "pherix_git_"));
  }

  commit(): void {
    this.cleanupBackupRoot();
  }

  rollback(): void {
    this.cleanupBackupRoot();
  }

  private cleanupBackupRoot(): void {
    if (this.backupRoot !== null) {
      rmSync(this.backupRoot, { recursive: true, force: true });
      this.backupRoot = null;
    }
  }

  // --- per-effect snapshot / apply / restore ------------------------------

  snapshot(effect: Effect): SnapshotHandle {
    if (this.backupRoot === null) {
      throw new Error(
        "GitAdapter.snapshot() called outside a transaction; begin() must be " +
          "called first.",
      );
    }
    const head = git(this.rootPath, ["rev-parse", "HEAD"], false) || null;
    // `git stash create` records the dirty *tracked* state as a commit object
    // without touching the worktree; empty output means clean.
    const stash = git(this.rootPath, ["stash", "create"], false) || "";
    // Untracked, non-ignored files are not in the stash — back them up.
    const untracked = git(this.rootPath, [
      "ls-files",
      "--others",
      "--exclude-standard",
    ])
      .split("\n")
      .filter((p) => p);
    const backupDir = path.join(this.backupRoot, `e_${effect.index}`);
    mkdirSync(backupDir, { recursive: true });
    for (const rel of untracked) {
      const src = path.join(this.rootPath, rel);
      const dst = path.join(backupDir, rel);
      if (existsSync(src)) {
        mkdirSync(path.dirname(dst), { recursive: true });
        copyFileSync(src, dst);
      }
    }
    const payload: GitPayload = { head, stash, backupDir, untracked };
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: payload as unknown as Record<string, unknown>,
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The handle is injected as the tool's first positional arg; the @tool
    // wrapper hides it from the agent's call-site (same shape as the FS adapter).
    return toolFn(new GitHandle(this.rootPath), effect.args);
  }

  restore(handle: SnapshotHandle): void {
    const payload = handle.payload as unknown as GitPayload;
    const head = payload.head;
    // 1. Tracked state + HEAD back to the snapshot commit (reflog-recoverable,
    //    so even commits the agent "destroyed" via reset --hard return).
    if (head) {
      git(this.rootPath, ["reset", "--hard", head]);
    }
    // 2. Remove every untracked file/dir (those the agent created after the
    //    snapshot, plus the snapshot-time ones — restored from backup next).
    git(this.rootPath, ["clean", "-fd"], false);
    // 3. Restore the untracked files that existed at snapshot time.
    const backupDir = payload.backupDir;
    for (const rel of payload.untracked ?? []) {
      const src = path.join(backupDir, rel);
      const dst = path.join(this.rootPath, rel);
      if (existsSync(src)) {
        mkdirSync(path.dirname(dst), { recursive: true });
        copyFileSync(src, dst);
      }
    }
    // 4. Re-apply the dirty tracked changes captured at snapshot time.
    const stash = payload.stash;
    if (stash) {
      git(this.rootPath, ["stash", "apply", stash], false);
    }
  }

  // --- isolation (VersionedResourceAdapter) -------------------------------
  //
  // Git tools record no per-key read/write sets, so the commit-time isolation
  // diff never queries these for a git effect. They are provided (returning the
  // current HEAD as the repo's version tag) so the adapter conforms honestly
  // rather than being a partial implementation.

  readVersion(_key: unknown): unknown {
    return git(this.rootPath, ["rev-parse", "HEAD"], false) || GIT_EMPTY;
  }

  writeVersion(_key: unknown): unknown {
    return git(this.rootPath, ["rev-parse", "HEAD"], false) || GIT_EMPTY;
  }
}
