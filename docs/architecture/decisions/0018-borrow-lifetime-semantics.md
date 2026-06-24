# ADR 0018 — Mutable-borrow lifetime is a dynamic exclusive borrow with a Store-backed atomic acquire

**Status:** Accepted · **Date:** 2026-06-23 · **Milestone:** F (Agent Language foundations)

> Issue CRA-200 / F-7. Resolves the PL/SECURITY fix flagged against ALG-4's
> "statically unaliasable exclusive borrow" claim (epic §F-7). Numbering: 0016 was
> the last accepted ADR; 0017 is reserved by a concurrent F-wave owner → this is **0018**.

## Context

The Agent Language treats a `Definition` as a frozen, reproducible artifact
(`versioning/version.py`, ADR 0012). Switching a definition into *train* / mutate
mode needs an **exclusive borrow**: while one holder mutates a definition, no other
holder anywhere may mutate the *same* definition concurrently. ALG-4 named this a
"statically unaliasable exclusive borrow".

That claim **overreaches**. A borrow that must hold across `async` tasks *and* across
separate processes (a `craw` CLI invocation and a daemon run sharing one Store)
cannot be proven unaliasable by Python's type system — there is no borrow checker,
and a runtime registry kept in an **in-process dict** is invisible to other processes
and racy across concurrently-scheduled async tasks. The original enforcement design
was therefore unsound: it would silently permit two concurrent mutable borrows.

The security spine cares about this directly. A mutable borrow is the write path to a
definition's content (prompts, tunable decode knobs — see F-5); concurrent unguarded
mutation is a correctness *and* an isolation hazard. Whatever enforces the borrow must
be **atomic** and **tenancy-scoped** (a borrow in org `a` must never touch org `b`).

The codebase already has exactly the primitive we need: `store.claim_idempotency(key,
*, org_id) -> bool` — an atomic, transactional, tenancy-scoped, race-safe check-then-claim
(`store/sqlite.py:107`, a single `INSERT OR IGNORE`), used by consequential sinks to
make side-effecting writes exactly-once (`nodes/sink.py:143`).

## Decision

**Downgrade the guarantee from "statically unaliasable" to a _dynamic exclusive borrow
with an atomic acquire_, and fix its lifetime with an explicit context-manager protocol.
Enforcement is a Store-backed atomic claim reusing `claim_idempotency` — never an
in-process dict.**

```python
with mutable(defn, store, org_id=...) as m:   # acquires on enter
    ...                                         # exclusive — no concurrent holder
# released on exit, even on exception
```

* **`mutable(target, store, *, org_id="local")`** — a `@contextmanager` factory in
  `crawfish/borrow.py`. Acquires on enter; releases in a `finally` on exit. Exposed as
  a **free function** (not a method on `Definition`) because another owner holds
  `definition/types.py` this wave; `Definition.mutable()` is a thin delegating method to
  be wired later (see changelog F-7).
* **`Borrowable`** — a structural `Protocol` (anything with a stable `.id`), so the
  module does not import `Definition`. `Definition.id` (deterministic per ADR 0006)
  satisfies it.
* The borrow key is **deterministic and identity-derived**: `borrow:<defn_id>:acq:<epoch>`.
  Tenancy is applied by the Store via `org_id`, so the same definition id in two orgs maps
  to two independent claims.

### The epoch + atomic-claim scheme (why it is race-safe *and* re-acquirable)

`claim_idempotency(key)` is **single-shot** — a key, once claimed, can never win again.
That is the perfect atomic gate for *one* acquisition but, naively, would make a borrow
un-re-acquirable after release. We close that with a monotonically increasing **epoch**
held in a `borrow_lock` record this module fully owns:

* **Acquire** reads the current epoch `e` (absent ⇒ `0`) and attempts
  `claim_idempotency("borrow:<id>:acq:<e>")`. **The claim is the gate**: if two tasks
  read the same `e`, exactly one wins; the loser raises `ExclusiveBorrowError`
  deterministically. The winner writes `{held: true, epoch: e}`.
* **Release** writes `{held: false, epoch: e + 1}`. The next acquire reads the bumped
  epoch and claims a *fresh* `acq:<e+1>` key — so round-trips never re-use, and never
  need to *delete*, an idempotency claim. (This is why we did not need an unowned
  `release_idempotency` store edit.)

Release is **idempotent** (double-release is a no-op), so the context manager's
`finally` is always safe, even after a manual `release()`.

Mutable borrows are thereby confined to a `train()`-style context that **cannot span
concurrent runs**: the lifetime is exactly the `with` block, and concurrency is rejected
at acquire.

## Alternatives rejected

- **In-process registry dict (the original ALG-4 design).** Invisible across processes;
  racy across async tasks (the check-then-set is not atomic without a lock the other
  process cannot see). Fails the only property the borrow must have. This ADR exists to
  replace it.
- **A static "unaliasable" type-system guarantee.** Python has no borrow checker; we
  cannot enforce single-writer at type-check time across processes. Honest weaker
  guarantee beats an unenforceable strong claim.
- **Editing the Store to add `release_idempotency` / a borrow table.** Out of scope this
  wave (store files are another owner's) and unnecessary — the epoch scheme makes release
  a plain `put_record` in a namespace this module owns, using only the public Store API.
- **A wall-clock lease / TTL on the claim.** Adds a liveness/expiry policy and a clock
  dependency for no benefit at this layer; the context manager already bounds the
  lifetime. A crash-recovery sweep (reaping a `held:true` record whose process died) can
  be layered later without changing this contract.
- **One claim key per definition, reused.** Impossible — `claim_idempotency` is
  single-shot, so the borrow could be acquired exactly once ever. Hence the epoch.

## Consequences

`mutable()` is the operational semantics behind `defn.mutable()`. A follow-up wires a
one-line `Definition.mutable(self, store, *, org_id="local")` method delegating to
`crawfish.borrow.mutable`. Because enforcement lives in the Store, the guarantee holds
across processes and survives the SQLite→Postgres driver swap unchanged (ADR 0001) — the
atomic claim is the seam, not the backend. The borrow is tenancy-scoped by construction,
upholding the spine's isolation rule. The one watch item is **holder-death recovery**: a
process that dies mid-borrow leaves `held:true` at epoch `e` with `acq:<e>` claimed;
re-acquire at `e` is correctly refused. A future reaper (or a lease TTL) can reclaim it
behind the same context-manager contract with no caller change.
