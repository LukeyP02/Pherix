# Generalization notes — the pressure log

When a stream (T1, T2, T3) finds the **frozen engine** won't let it express
something cleanly, it records the pressure here instead of patching the substrate.
`A7` harvests every file in this directory into **one coherent generalisation + slim
pass** of both engines — harvest-driven only, mirrored Python↔TS.

One file per stream: `ts-adapters-a.md`, `ts-adapters-b.md`, `parity.md`.
Append-only — never rewrite another stream's file.

Each entry:

```
### <short title>
- **Doing:** what the axis work was trying to express
- **Resisted:** what the frozen engine wouldn't let you do
- **Smallest fix:** the minimal substrate generalisation that would (your guess)
```

If two streams log the same pressure, that is the *confirmed* abstraction — A7
generalises it. If a note asks for a one-off engine branch, A7 finds the general
form or declines it with a one-line reason. The engine must come out **smaller and
more general**, never more bloated.
