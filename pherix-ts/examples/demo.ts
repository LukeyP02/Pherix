/**
 * Worked example — run with `npm run example` (tsx examples/demo.ts).
 *
 * Two scenarios, mirroring the Python README demo:
 *   1. A reversible DB write that rolls back — the snapshot restores prior state.
 *   2. An irreversible call that gates — commit blocks without approval, so the
 *      external side-effect never fires.
 */

import Database from "better-sqlite3";
import {
  GateBlocked,
  HttpAdapter,
  SqliteAdapter,
  StagedResult,
  agentTxn,
  tool,
  type SqliteDatabase,
} from "../src/index.js";

// --- set up a real resource + adapters ------------------------------------

const db = new Database(":memory:");
db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
db.prepare("INSERT INTO accounts VALUES (?, ?)").run("alice", 100);
db.prepare("INSERT INTO accounts VALUES (?, ?)").run("bob", 0);

const adapters = { sql: new SqliteAdapter(db), http: new HttpAdapter() };

const balance = (name: string): number =>
  (db.prepare("SELECT balance FROM accounts WHERE name = ?").get(name) as { balance: number })
    .balance;

// --- register tools --------------------------------------------------------

// Reversible: SQL adapter injects the connection as the first arg.
const transfer = tool<{ from: string; to: string; amount: number }>(
  "sql",
  (conn: SqliteDatabase, args) => {
    conn.prepare("UPDATE accounts SET balance = balance - ? WHERE name = ?").run(args.amount, args.from);
    conn.prepare("UPDATE accounts SET balance = balance + ? WHERE name = ?").run(args.amount, args.to);
    return { ok: true };
  },
  { name: "transfer" },
);

// Irreversible: an HTTP call with no registered compensator — it must gate.
let emailsSent = 0;
const sendEmail = tool<{ to: string; body: string }>(
  "http",
  (args) => {
    emailsSent += 1;
    console.log(`    [external] email actually sent to ${args.to}`);
    return { delivered: true };
  },
  { name: "sendEmail", injectsHandle: false },
);

// --- scenario 1: reversible write that rolls back --------------------------

async function reversibleRollback(): Promise<void> {
  console.log("1) reversible DB write that rolls back");
  console.log(`   before:  alice=${balance("alice")} bob=${balance("bob")}`);
  const ctx = await agentTxn(adapters, (txn) => {
    transfer({ from: "alice", to: "bob", amount: 30 });
    console.log(`   mid-txn: alice=${balance("alice")} bob=${balance("bob")} (applied live)`);
    txn.rollback(); // the agent changes its mind
  });
  console.log(`   after:   alice=${balance("alice")} bob=${balance("bob")} (state=${ctx.txn.state})`);
  console.log("   → the snapshot restored the prior state; the write left no trace.\n");
}

// --- scenario 2: irreversible call that gates ------------------------------

async function irreversibleGate(): Promise<void> {
  console.log("2) irreversible call that gates");
  try {
    await agentTxn(adapters, () => {
      const staged = sendEmail({ to: "user@example.com", body: "hello" });
      console.log(`   staged: ${(staged as StagedResult).toString()} (not sent yet)`);
      // No approveIrreversible(...) call → commit must block at the gate.
    });
  } catch (e) {
    if (e instanceof GateBlocked) {
      console.log(`   commit blocked at the gate; needs approval: ${e.needsApproval.join(", ")}`);
    } else {
      throw e;
    }
  }
  console.log(`   emails actually sent: ${emailsSent}`);
  console.log("   → the irreversible effect never fired because nobody approved it.\n");
}

await reversibleRollback();
await irreversibleGate();
console.log("Both guarantees held: rollback restored state; the gate stopped an un-approved irreversible.");
