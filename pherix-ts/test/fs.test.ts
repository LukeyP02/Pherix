/** Mirrors tests/test_adapters_filesystem.py — the FS adapter's copy-on-write
 *  lane: rollback round-trip, path containment, lazy first-touch, key recording. */

import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  FilesystemAdapter,
  type FsHandle,
  REGISTRY,
  TxnState,
  agentTxn,
  tool,
} from "../src/index.js";

let root: string;
let fs: FilesystemAdapter;

const dec = new TextDecoder();
const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

// The tool REGISTRY is module-level singleton state, so each test clears it and
// re-registers fresh tools — exactly the helpers.ts discipline.
beforeEach(() => {
  REGISTRY.clear();
  root = mkdtempSync(path.join(tmpdir(), "pherix_fs_test_"));
  fs = new FilesystemAdapter(root);
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

function fileText(rel: string): string {
  return readFileSync(path.join(root, rel), "utf8");
}

function fsTools() {
  return {
    writeFile: tool<{ rel: string; text: string }>(
      "fs",
      (h: FsHandle, args) => {
        h.write(args.rel, enc(args.text));
        return { ok: true };
      },
      { name: "writeFile" },
    ),
    deleteFile: tool<{ rel: string }>(
      "fs",
      (h: FsHandle, args) => {
        h.delete(args.rel);
        return { ok: true };
      },
      { name: "deleteFile" },
    ),
    readFile: tool<{ rel: string }>("fs", (h: FsHandle, args) => dec.decode(h.read(args.rel)), {
      name: "readFile",
    }),
  };
}

describe("FilesystemAdapter rollback round-trip", () => {
  it("rollback restores a modified file's original content", async () => {
    writeFileSync(path.join(root, "a.txt"), "original");
    const t = fsTools();
    const ctx = await agentTxn({ fs }, async (txn) => {
      await t.writeFile({ rel: "a.txt", text: "changed" });
      expect(fileText("a.txt")).toBe("changed"); // applied live, mid-txn
      await txn.rollback();
    });
    expect(ctx.txn.state).toBe(TxnState.ROLLED_BACK);
    expect(fileText("a.txt")).toBe("original");
  });

  it("rollback removes a file the txn created", async () => {
    const t = fsTools();
    await agentTxn({ fs }, async (txn) => {
      await t.writeFile({ rel: "new.txt", text: "hello" });
      expect(existsSync(path.join(root, "new.txt"))).toBe(true);
      await txn.rollback();
    });
    expect(existsSync(path.join(root, "new.txt"))).toBe(false);
  });

  it("rollback restores a file the txn deleted", async () => {
    writeFileSync(path.join(root, "doomed.txt"), "keep me");
    const t = fsTools();
    await agentTxn({ fs }, async (txn) => {
      await t.deleteFile({ rel: "doomed.txt" });
      expect(existsSync(path.join(root, "doomed.txt"))).toBe(false);
      await txn.rollback();
    });
    expect(fileText("doomed.txt")).toBe("keep me");
  });

  it("clean exit commits the write (it persists)", async () => {
    const t = fsTools();
    const ctx = await agentTxn({ fs }, async () => {
      await t.writeFile({ rel: "kept.txt", text: "durable" });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(fileText("kept.txt")).toBe("durable");
  });

  it("multi-effect rollback folds back newest-first to the original", async () => {
    writeFileSync(path.join(root, "a.txt"), "v0");
    const t = fsTools();
    await agentTxn({ fs }, async (txn) => {
      await t.writeFile({ rel: "a.txt", text: "v1" });
      await t.writeFile({ rel: "a.txt", text: "v2" });
      expect(fileText("a.txt")).toBe("v2");
      await txn.rollback();
    });
    expect(fileText("a.txt")).toBe("v0");
  });
});

describe("FilesystemAdapter path containment", () => {
  it("rejects a path escaping the root via ..", async () => {
    const t = fsTools();
    await expect(
      agentTxn({ fs }, async () => {
        await t.writeFile({ rel: "../escape.txt", text: "x" });
      }),
    ).rejects.toThrow(/outside root/);
    expect(existsSync(path.join(root, "..", "escape.txt"))).toBe(false);
  });

  it("rejects an absolute path", async () => {
    const t = fsTools();
    await expect(
      agentTxn({ fs }, async () => {
        await t.writeFile({ rel: "/etc/pwn", text: "x" });
      }),
    ).rejects.toThrow(/outside root/);
  });
});

describe("FilesystemAdapter key recording", () => {
  it("records a read-key (content hash) and write-keys onto the effect", async () => {
    writeFileSync(path.join(root, "seed.txt"), "seed");
    const rw = tool<{ rel: string }>(
      "fs",
      (h: FsHandle, args) => {
        const data = h.read(args.rel);
        h.write(args.rel, new Uint8Array([...data, ...enc("!")]));
        return { ok: true };
      },
      { name: "readWrite" },
    );
    const ctx = await agentTxn({ fs }, async () => {
      await rw({ rel: "seed.txt" });
    });
    const eff = ctx.txn.effects[0]!;
    expect(eff.readKeys).toHaveLength(1);
    expect(eff.readKeys[0]![0]).toBe("fs");
    // a read-key version is the sha256 of the pre-write content, not __missing__
    expect(eff.readKeys[0]![2]).not.toBe("__missing__");
    expect(eff.writeKeys.length).toBeGreaterThanOrEqual(1);
  });
});

describe("FilesystemAdapter state diff (StateDiffable)", () => {
  it("reports added / modified / deleted against a baseline", () => {
    mkdirSync(path.join(root, "sub"), { recursive: true });
    writeFileSync(path.join(root, "keep.txt"), "k");
    writeFileSync(path.join(root, "mod.txt"), "before");
    writeFileSync(path.join(root, "gone.txt"), "g");
    const baseline = fs.stateBaseline();

    writeFileSync(path.join(root, "mod.txt"), "after");
    writeFileSync(path.join(root, "sub", "new.txt"), "n");
    rmSync(path.join(root, "gone.txt"));

    const diff = fs.stateDiff(baseline) as Record<string, string[]>;
    expect(diff.files_added).toContain("sub/new.txt");
    expect(diff.files_modified).toContain("mod.txt");
    expect(diff.files_deleted).toContain("gone.txt");
    expect(diff.files_added).not.toContain("keep.txt");
  });
});
