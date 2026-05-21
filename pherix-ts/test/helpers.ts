/**
 * Shared test fixtures. The tool REGISTRY is module-level singleton state
 * (exactly like Python's), so every test clears it and re-registers fresh tools
 * via `setup()` in a beforeEach — avoiding "already registered" across tests.
 */

import Database from "better-sqlite3";
import {
  HttpAdapter,
  REGISTRY,
  SqliteAdapter,
  tool,
  type ResourceAdapter,
  type SqliteDatabase,
} from "../src/index.js";

export interface SideEffectLog {
  /** External calls an HTTP tool actually fired — empty until commit. */
  sent: Array<{ tool: string; args: Record<string, unknown> }>;
}

export interface Fixture {
  db: Database.Database;
  sql: SqliteAdapter;
  http: HttpAdapter;
  adapters: Record<string, ResourceAdapter>;
  log: SideEffectLog;
  balanceOf(name: string): number;
  tools: {
    transfer: (args: { from: string; to: string; amount: number }) => unknown;
    setBalance: (args: { name: string; balance: number }) => unknown;
    boom: (args: Record<string, unknown>) => unknown;
    sendEmail: (args: { to: string; body: string }) => unknown;
    charge: (args: { card: string; amount: number }) => unknown;
    refund: (args: { card: string; amount: number }) => unknown;
  };
}

/** Build a fresh in-memory DB, adapters, and a fresh set of registered tools. */
export function setup(): Fixture {
  REGISTRY.clear();

  const db = new Database(":memory:");
  db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("alice", 100);
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("bob", 0);

  const sql = new SqliteAdapter(db);
  const http = new HttpAdapter();
  const adapters: Record<string, ResourceAdapter> = { sql, http };
  const log: SideEffectLog = { sent: [] };

  // Reversible SQL tools — the connection is the injected handle. Tools pass an
  // explicit name: bundlers (esbuild/vitest) mangle a named function expression
  // assigned to a same-named const (`charge` -> `charge2`), so a derived
  // fn.name is not reliable for compensator string references.
  const transfer = tool<{ from: string; to: string; amount: number }>(
    "sql",
    (conn: SqliteDatabase, args) => {
      conn
        .prepare("UPDATE accounts SET balance = balance - ? WHERE name = ?")
        .run(args.amount, args.from);
      conn
        .prepare("UPDATE accounts SET balance = balance + ? WHERE name = ?")
        .run(args.amount, args.to);
      return { ok: true };
    },
    { name: "transfer" },
  );

  const setBalance = tool<{ name: string; balance: number }>(
    "sql",
    (conn: SqliteDatabase, args) => {
      conn
        .prepare("UPDATE accounts SET balance = ? WHERE name = ?")
        .run(args.balance, args.name);
      return { ok: true };
    },
    { name: "setBalance" },
  );

  const boom = tool(
    "sql",
    (_conn: SqliteDatabase, _args: Record<string, unknown>) => {
      throw new Error("boom: tool failed during apply");
    },
    { name: "boom" },
  );

  // Irreversible HTTP tools — no injected handle; fire only at commit.
  const sendEmail = tool<{ to: string; body: string }>(
    "http",
    (args) => {
      log.sent.push({ tool: "sendEmail", args });
      return { delivered: true };
    },
    { injectsHandle: false, name: "sendEmail" },
  );

  const refund = tool<{ card: string; amount: number }>(
    "http",
    (args) => {
      log.sent.push({ tool: "refund", args });
      return { refunded: true };
    },
    { injectsHandle: false, name: "refund" },
  );

  const charge = tool<{ card: string; amount: number }>(
    "http",
    (args) => {
      log.sent.push({ tool: "charge", args });
      return { charged: true };
    },
    { injectsHandle: false, name: "charge", compensator: "refund" },
  );

  return {
    db,
    sql,
    http,
    adapters,
    log,
    balanceOf(name: string): number {
      const row = db
        .prepare("SELECT balance FROM accounts WHERE name = ?")
        .get(name) as { balance: number } | undefined;
      return row?.balance ?? 0;
    },
    tools: { transfer, setBalance, boom, sendEmail, charge, refund },
  };
}
