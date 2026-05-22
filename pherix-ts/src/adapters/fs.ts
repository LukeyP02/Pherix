/**
 * FilesystemAdapter — copy-on-write backup over a rooted directory.
 * Mirror of pherix/core/adapters/filesystem.py.
 *
 * This adapter proves the ResourceAdapter protocol is a real abstraction by
 * satisfying it with machinery structurally unlike SQL: file copies into a
 * per-txn tempdir, not database savepoints. It conforms to
 * TransactionalResourceAdapter (begin/commit/rollback bracket the per-txn
 * backup root) and to the per-effect snapshot -> apply -> restore lifecycle,
 * and opts into StateDiffable for dry-run preview.
 *
 * Lazy snapshot rule: backups are taken at *first touch* of a path within a
 * single effect. Reads never trigger backups; subsequent writes/deletes to the
 * same path inside the same effect do not re-backup (the pre-effect state is
 * already captured). Across effects, each effect carries its own backup record
 * — so a backward fold (newest-first) restores effect N's pre-state, then
 * N-1's, landing at the original.
 */

import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  unlinkSync,
  writeFileSync,
  type Dirent,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Effect, SnapshotHandle } from "../effects.js";
import type { StateDiffable, ToolFn, TransactionalResourceAdapter } from "./base.js";
import { activeEffect } from "../tools.js";

/**
 * Sentinel returned by `readVersion` when the path does not exist. A non-null
 * marker means the isolation diff can distinguish "I read this file as absent"
 * from a sha256 hash via a plain `!==` comparison — a later create then
 * correctly conflicts.
 */
const FS_MISSING = "__missing__";

interface TouchRecord {
  backup: string | null;
  existed: boolean;
}

/** The per-effect filesystem handle injected as the first arg of FS tools.
 *
 * Resolves every relative path against `root`, rejects anything that escapes it
 * (`..` segments, absolute paths, symlinks pointing elsewhere), and records
 * first-touch backups into the effect's backup subdirectory. Subsequent touches
 * of the same path are pass-through writes — the pre-effect state is already
 * captured.
 *
 * Every `read` records a read-key `(path, content-hash)` and every `write` /
 * `delete` records a write-key into the bound Effect. Recording is a no-op when
 * `effect` is null (the handle still functions for raw unit tests outside
 * agentTxn). Per-handle dedupe sets ensure re-reading the same path inside one
 * effect does not bloat the journal — the first read's version is the one the
 * agent's logic branched on. */
export class FsHandle {
  private readonly recordedReads = new Set<string>();
  private readonly recordedWrites = new Set<string>();

  constructor(
    private readonly root: string,
    private readonly backupDir: string,
    private readonly touched: Record<string, TouchRecord>,
    private readonly effect: Effect | null,
    private readonly adapter: FilesystemAdapter | null,
  ) {}

  // --- public API (tool-facing) ---

  write(relPath: string, data: Uint8Array): void {
    const target = this.safePath(relPath);
    this.recordFirstTouch(relPath, target);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, data);
    this.recordWriteKey(relPath);
  }

  read(relPath: string): Buffer {
    const target = this.safePath(relPath);
    // Reads do not trigger backups — they don't change state.
    const data = readFileSync(target);
    this.recordReadKey(relPath);
    return data;
  }

  delete(relPath: string): void {
    const target = this.safePath(relPath);
    this.recordFirstTouch(relPath, target);
    // If the file didn't exist pre-effect, the record above captured
    // "existed: false" and we still want a hard error here — the agent asked
    // us to delete something that wasn't there.
    unlinkSync(target);
    this.recordWriteKey(relPath);
  }

  // --- internals ---

  private safePath(relPath: string): string {
    return resolveWithinRoot(this.root, relPath);
  }

  private recordFirstTouch(relPath: string, absPath: string): void {
    if (relPath in this.touched) return; // lazy: pre-effect state already captured
    if (existsSync(absPath)) {
      const backupName = `${createHash("sha256").update(`${relPath}:${Date.now()}:${Math.random()}`).digest("hex")}.bin`;
      copyFileSync(absPath, path.join(this.backupDir, backupName));
      this.touched[relPath] = { backup: backupName, existed: true };
    } else {
      this.touched[relPath] = { backup: null, existed: false };
    }
  }

  // --- isolation recording ---

  private recordReadKey(relPath: string): void {
    if (this.effect === null || this.adapter === null) return;
    if (this.recordedReads.has(relPath)) return;
    const version = this.adapter.readVersion([relPath]);
    this.effect.readKeys.push(["fs", [relPath], version]);
    this.recordedReads.add(relPath);
  }

  private recordWriteKey(relPath: string): void {
    // Re-hash AFTER the write has landed so the version we record is what the
    // adapter would report on readVersion right now. Writes are NOT
    // deduplicated: the last entry carries the freshest post-write version.
    if (this.effect === null || this.adapter === null) return;
    const versionAfter = this.adapter.readVersion([relPath]);
    this.effect.writeKeys.push(["fs", [relPath], versionAfter]);
    this.recordedWrites.add(relPath);
  }
}

/** Resolve `relPath` against `root`, rejecting any escape. Shared by the handle
 *  and the adapter's version/diff lookups so the safety story is uniform. */
function resolveWithinRoot(root: string, relPath: string): string {
  if (path.isAbsolute(relPath)) {
    throw new Error(`path ${JSON.stringify(relPath)} is outside root ${root}`);
  }
  const resolved = path.resolve(root, relPath);
  const rel = path.relative(root, resolved);
  // `..` prefix or an absolute rel means the candidate escapes root (catches
  // `../` segments). Empty rel (== root itself) is also rejected — a tool
  // addresses files under root, not root.
  if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`path ${JSON.stringify(relPath)} is outside root ${root}`);
  }
  // Symlink escape: if the path (or an ancestor) exists as a symlink whose real
  // target leaves root, reject — mirrors Python's .resolve() following links.
  let probe = resolved;
  while (probe !== root && probe.length > root.length) {
    if (existsSync(probe)) {
      const real = realpathSync(probe);
      const realRel = path.relative(realpathSync(root), real);
      if (realRel.startsWith("..") || path.isAbsolute(realRel)) {
        throw new Error(`path ${JSON.stringify(relPath)} resolves outside root ${root}`);
      }
      break;
    }
    probe = path.dirname(probe);
  }
  return resolved;
}

export class FilesystemAdapter implements TransactionalResourceAdapter, StateDiffable {
  readonly name = "fs";
  private readonly rootPath: string;
  private backupRoot: string | null = null;

  constructor(root: string) {
    // Resolve root once on construction; the handle's safe-path check compares
    // resolved-to-resolved (symlinks under root still work, but symlinks
    // pointing outside it are rejected).
    this.rootPath = realpathSync(path.resolve(root));
  }

  get root(): string {
    return this.rootPath;
  }

  get backupRootDir(): string | null {
    return this.backupRoot;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- transaction-scope lifecycle ---

  begin(): void {
    this.backupRoot = mkdtempSync(path.join(tmpdir(), "pherix_fs_"));
  }

  commit(): void {
    this.cleanupBackupRoot();
  }

  rollback(): void {
    this.cleanupBackupRoot();
  }

  private cleanupBackupRoot(): void {
    if (this.backupRoot !== null) {
      rmSync(this.backupRoot, { recursive: true, force: false });
      this.backupRoot = null;
    }
  }

  // --- per-effect snapshot / apply / restore ---

  snapshot(effect: Effect): SnapshotHandle {
    if (this.backupRoot === null) {
      throw new Error(
        "FilesystemAdapter.snapshot() called outside a transaction; " +
          "begin() must be called first.",
      );
    }
    const backupDir = path.join(this.backupRoot, `e_${effect.index}`);
    mkdirSync(backupDir);
    // `touched` is mutated by the FsHandle during apply(); keeping it in the
    // payload means the audit journal sees the final list of touched paths once
    // the effect status flips to APPLIED.
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { backupDir, touched: {} as Record<string, TouchRecord> },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    const handle = this.handleFor(effect.snapshot!);
    // The handle is injected as the tool's first arg; the tool wrapper hides it
    // from the agent's call-site, then passes the named-args object.
    return toolFn(handle, effect.args);
  }

  restore(handle: SnapshotHandle): void {
    const backupDir = handle.payload["backupDir"] as string;
    const touched = handle.payload["touched"] as Record<string, TouchRecord>;
    for (const [relPath, record] of Object.entries(touched)) {
      const target = path.join(this.rootPath, relPath);
      if (record.existed) {
        copyFileSync(path.join(backupDir, record.backup as string), target);
      } else if (existsSync(target)) {
        // Pre-state was "didn't exist" — delete whatever's there now.
        unlinkSync(target);
      }
    }
  }

  // --- handle construction ---

  private handleFor(snapshot: SnapshotHandle): FsHandle {
    // Pass the active Effect (and self) so the FsHandle can record read/write
    // keys automatically. `activeEffect` is set by the runtime around
    // adapter.apply; outside an agentTxn it is undefined and the handle skips
    // recording.
    return new FsHandle(
      this.rootPath,
      snapshot.payload["backupDir"] as string,
      snapshot.payload["touched"] as Record<string, TouchRecord>,
      activeEffect.getStore() ?? null,
      this,
    );
  }

  // --- versioning ---

  readVersion(key: unknown[]): string {
    if (key.length !== 1) {
      throw new Error(
        `FilesystemAdapter version key must be a 1-tuple (relPath,); got ${JSON.stringify(key)}`,
      );
    }
    const target = resolveWithinRoot(this.rootPath, key[0] as string);
    if (!existsSync(target)) return FS_MISSING;
    return createHash("sha256").update(readFileSync(target)).digest("hex");
  }

  writeVersion(key: unknown[]): string {
    // Compute from on-disk content *after* the write — no cache.
    return this.readVersion(key);
  }

  // --- state diff (StateDiffable) ---

  private walkHashes(): Record<string, string> {
    // `{relpath: sha256}` over every file under root. The per-txn backup root
    // lives in a system tempdir (begin() mkdtemp), not under root, so it cannot
    // pollute the walk. relpath is POSIX-style so diff keys match the relPath
    // strings tools pass to FsHandle.
    const out: Record<string, string> = {};
    if (!existsSync(this.rootPath)) return out;
    const walk = (dir: string): void => {
      for (const entry of readdirEntries(dir)) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
        } else if (entry.isFile()) {
          const rel = path.relative(this.rootPath, full).split(path.sep).join("/");
          out[rel] = createHash("sha256").update(readFileSync(full)).digest("hex");
        }
      }
    };
    walk(this.rootPath);
    return out;
  }

  stateBaseline(): Record<string, string> {
    return this.walkHashes();
  }

  stateDiff(baseline: unknown): Record<string, unknown> {
    const base = baseline as Record<string, string>;
    const now = this.walkHashes();
    const filesAdded = Object.keys(now).filter((rel) => !(rel in base));
    const filesModified = Object.keys(now).filter(
      (rel) => rel in base && base[rel] !== now[rel],
    );
    const filesDeleted = Object.keys(base).filter((rel) => !(rel in now));
    return {
      files_added: filesAdded,
      files_modified: filesModified,
      files_deleted: filesDeleted,
    };
  }
}

/** withFileTypes gives the entry kind without an extra stat per file. */
function readdirEntries(dir: string): Dirent[] {
  return readdirSync(dir, { withFileTypes: true });
}
