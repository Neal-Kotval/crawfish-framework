# Sandbox & jail

The host-side leg of the security spine: how *your* and *users'* node code runs
isolated from the engine, scoped to a folder, with the network shut off — and how the
untrusted-data tag (taint) follows values across that process edge. These live in
`crawfish.jail` and `crawfish.sandbox`.

**Symbols on this page:** `Jail` · `FakeJail` · `NoJail` · `BwrapJail` · `SeatbeltJail` ·
`JailPath` · `PathMode` · `JailResult` · `Denial` · `DenialKind` · `SandboxPolicy` ·
`StaticOnlyError` · `UnsupportedPlatformError` · `select_jail` · `registry_descriptors` ·
`rehydrate_registry` · `emit_denials` · `EgressBroker` · `EgressDenied` ·
`run_out_of_process` · `TaintSet`

---

## Core

Some pipeline nodes — a source that reads files, a sink that writes them, a filter that
shells out — run **host-side Python**: code on your machine, not the `claude -p` child.
That code can be reached by **fluid** input — *untrusted session data that came from
outside your control* (a ticket body, a fetched page). A prompt injection riding in on
fluid data could try to make such a node read `/etc/shadow` or phone home. So host-side
node code does not run inside the engine. It runs **out-of-process** — in a *separate OS
process that shares none of the engine's memory or credentials* — inside a **jail**.

A **jail** does three things to that separate process:

- **Folder scope** — it can only see paths you explicitly allow. Everything else is
  invisible.
- **Network denied by default** — no outbound connections unless you opt in.
- **Taint propagation** — *taint* is a label that marks a value as untrusted. If the
  jailed child read fluid data, called a tool, or touched the network, every value it
  hands back comes out tagged untrusted, so downstream code knows not to trust it.

`Jail` is the abstract contract. The concrete backends are OS-specific: **`BwrapJail`**
on Linux (the `bwrap` sandbox), **`SeatbeltJail`** on macOS (`sandbox-exec`). For tests
there is **`FakeJail`**, which spawns nothing but applies the exact same allow/deny
policy in-process; and **`NoJail`**, an explicit opt-out for code that is provably never
reached by fluid data. **`select_jail`** picks the right backend for the current
platform, or raises **`UnsupportedPlatformError`** where none exists (Windows).

You declare what the jail may reach with a **`JailPath`** — a path plus a **`PathMode`**
(read-only or read-write). The full ruleset is a **`SandboxPolicy`**. When the child
tries to step outside the rules, the jail records a **`Denial`** (its **`DenialKind`**
says why) and surfaces it on the run's **`JailResult`**. **`emit_denials`** writes those
denials to the audit ledger.

The single hard rule: the allow-list of folders is **static-only** — each allowed path
may only come from trusted node configuration, never from fluid data. A fluid value can
never widen the jail. Offer a fluid path where only static is allowed and you get a
**`StaticOnlyError`**. (Whether the network is open is a plain static flag with no
per-value provenance — it cannot carry a fluid value in the first place.)

A **`TaintSet`** is just the set of those untrusted-labels on a value. **`EgressBroker`**
is the network allow-list host-side code checks before connecting (**`EgressDenied`** if
the host isn't on it). **`run_out_of_process`** is the primitive that actually runs a
function in a child process.

---

## Ramps up

### One contract, OS-specific backends (ADR 0016)

The enforcement primitive is unavoidably OS-sensitive, so the design is a single `Jail`
ABC with one `run` contract and per-platform backends rather than one cross-platform
mechanism. See [ADR 0016](../architecture/decisions/0016-sandbox-isolation-mechanism.md) for the choice and rejected
alternatives.

- **`BwrapJail`** (Linux) wraps the command in `bwrap`: `--unshare-net` makes loopback
  the only reachable network (no egress path exists), `--ro-bind`/`--bind` are the folder
  allow-list, and a fresh user namespace drops ambient authority. OS runtime dirs
  (`/usr`, `/lib`, …) are bound **read-only** so the interpreter can start — they never
  widen the writable scope.
- **`SeatbeltJail`** (macOS) renders an SBPL profile — `(deny default)` then
  `(allow file-read*/file-write* (subpath …))` per `JailPath`, and `(deny network*)`
  unless net is granted — and runs under `sandbox-exec`. `sandbox-exec` is
  deprecated-but-present; the warning goes to stderr, the mechanism still enforces.
- **`FakeJail`** spawns nothing. A test injects a *program* describing the paths the
  child would touch and the hosts it would connect to; the fake applies **exactly** the
  policy a real backend enforces and records the same denials. A conformance suite runs
  one body against the fake and the real backends so the fake can't drift.
- **`NoJail`** still runs out-of-process and still propagates taint, but enforces no
  folder or network scope. It is the explicit opt-out — never the default for fluid code.

### `select_jail` and unsupported platforms

`select_jail` sniffs the OS: Linux → `BwrapJail`, macOS → `SeatbeltJail`. A
`SandboxPolicy.kind` of `'bwrap'`, `'seatbelt'`, `'fake'`, or `'nojail'` pins a backend
(used by tests and the opt-out). Windows has no clean unprivileged primitive and is
deferred (ADR 0009), so it raises `UnsupportedPlatformError`.

### Static-only is enforced before any process spawns

`allow_paths` and `allow_net` derive from static node config only. Every backend calls
the shared `Jail._check_static(allow_paths)` first thing in `run`: a `JailPath` whose
`flow` is `Flow.FLUID` raises `StaticOnlyError` **before** a child is launched. This is
the spine rule made executable — a fluid (untrusted) value can never widen the jail.
(`allow_net` is a bare `bool` with no `flow` field, so it is static by construction; the
check covers the one input that *could* carry provenance — `allow_paths`.)

### The child is the taint boundary

Input `taint` is serialized into the child; every value crossing back is re-tagged via
`JailResult.out_taint`. Under `FakeJail`, the result is tagged `"fluid"` (the
`FLUID_TAINT` label) if the child's output derives from fluid input, if it connected to
the network, or if it produced any denial — a denial is itself evidence the child reached
for untrusted scope. The real backends carry input taint forward and, when network egress
was explicitly granted, presumptively taint the output (a net-granted child may have
pulled in untrusted remote data); under the default deny-net there is no egress path.

### Denials are blocked *and* audited

A blocked escape is a `Denial` on `JailResult.denied`. `emit_denials` writes one
`JAIL_VIOLATION` emission per denial to the store, each carrying the required `attempt`
and `severity` attrs and marked `tainted=True` (a denial is by definition untrusted
code's attempt). "Blocked and audited" is the broker's contract and feeds the red-team
demo and dashboard.

### Type registry crosses the boundary

The jailed child is a fresh process and cannot inherit Python identities, so structural
type compatibility would break across the edge. `registry_descriptors` serializes the
[type registry](type-system.md)'s records to JSON descriptors; the child calls
`rehydrate_registry` at startup to reconstruct them, so `parameters_compatible` behaves
identically on both sides.

### `EgressBroker` is the Phase-1 network floor

`EgressBroker` is a cooperative allow-list: host-side code calls `broker.guard(host)`
before connecting and gets `EgressDenied` if the host isn't permitted. It is the portable
Phase-1 floor, not the final boundary — a transparent interception layer that blocks
undeclared egress even without a cooperative call, plus full microVM/seccomp isolation,
is tracked separately.

---

## API reference

### `TaintSet`

`TaintSet = frozenset[str]` — a set of opaque untrusted-labels on a value. The presence
of **any** label means untrusted. Serialized across the process boundary as a JSON list,
so labels stay JSON-primitive. The canonical label is the module constant
`FLUID_TAINT = "fluid"`.

### `PathMode`

`class PathMode(str, Enum)` — access mode for an allowed path.

| Member | Value | Meaning |
| --- | --- | --- |
| `PathMode.RO` | `"ro"` | Read-only. |
| `PathMode.RW` | `"rw"` | Read-write. |

### `JailPath`

`@dataclass(frozen=True) class JailPath` — a host path made reachable inside the jail.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `path` | `str` | — (required) | Host path to expose. |
| `mode` | `PathMode` | `PathMode.RO` | Read-only or read-write. |
| `flow` | `Flow` | `Flow.STATIC` | Provenance. A `Flow.FLUID` path is rejected with `StaticOnlyError` — fluid can never widen the jail. |

Method: `contains(candidate: str) -> bool` — `True` if `candidate` is this path or lives
beneath it (no escape).

### `DenialKind`

`class DenialKind(str, Enum)` — why an attempt was blocked.

| Member | Value | Meaning |
| --- | --- | --- |
| `DenialKind.FOLDER_ESCAPE` | `"folder_escape"` | Read/write outside `allow_paths`. |
| `DenialKind.UNDECLARED_EGRESS` | `"undeclared_egress"` | Network connect with `allow_net=False`. |
| `DenialKind.TIMEOUT` | `"timeout"` | Wall-clock budget exceeded. |

### `Denial`

`@dataclass(frozen=True) class Denial` — one audited escape attempt the jail blocked.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | `DenialKind` | — (required) | Why it was blocked. |
| `attempt` | `str` | — (required) | The path or `host:port` the child tried to reach. |
| `severity` | `str` | `"high"` | Severity tag; a blocked escape is security-relevant. |

Method: `as_attrs() -> dict[str, object]` — the `attrs` payload (`attempt`, `severity`,
`kind`) for a `JAIL_VIOLATION` emission.

### `JailResult`

`@dataclass(frozen=True) class JailResult` — the frozen result of a jailed run.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `exit_code` | `int` | — (required) | Child exit code; nonzero if it escaped. |
| `stdout` | `bytes` | — (required) | Captured stdout. |
| `stderr` | `bytes` | — (required) | Captured stderr. |
| `out_taint` | `TaintSet` | `frozenset()` | Taint propagated back out of the child. |
| `denied` | `tuple[Denial, ...]` | `()` | Every escape the jail blocked. |
| `timed_out` | `bool` | `False` | Whether the wall-clock budget was exceeded. |

### `SandboxPolicy`

`@dataclass(frozen=True) class SandboxPolicy` — static config that selects and
parameterizes the jail.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | `str \| None` | `None` | Pins a backend (`'bwrap'`, `'seatbelt'`, `'fake'`, `'nojail'`); `None` lets `select_jail` sniff the OS. |
| `allow_net` | `bool` | `False` | Policy network default; a per-run `allow_net` may only narrow it. Both are static. |

### `StaticOnlyError`

`class StaticOnlyError(ValueError)` — raised when a `Flow.FLUID` value is offered where
only static is permitted (a fluid `JailPath` in `allow_paths`). A fluid value can never widen the
jail.

### `UnsupportedPlatformError`

`class UnsupportedPlatformError(RuntimeError)` — raised by `select_jail` on a platform
with no real backend (Windows).

### `Jail`

`class Jail(ABC)` — out-of-process, folder-scoped, network-denied execution of host-side
node code. A behavioural ABC (ADR 0004), imported by the node runner — never a concrete
backend imported directly.

```python
def run(
    self,
    cmd: Sequence[str],
    *,
    allow_paths: Sequence[JailPath] = (),
    allow_net: bool = False,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    cwd: JailPath | str | None = None,
    timeout_s: float | None = None,
    taint: TaintSet = frozenset(),
) -> JailResult
```

Abstract property `kind -> str` — backend tag (`'bwrap' | 'seatbelt' | 'fake' |
'nojail'`). Static helper `Jail._check_static(allow_paths)` rejects any `Flow.FLUID`
allow path; every backend calls it first.

### `FakeJail`

`class FakeJail(Jail)` — in-process fake honouring the same observable policy as a real
backend; the default in unit tests. `kind == "fake"`.

```python
FakeJail(program: Callable[[Sequence[str]], _Probe] | None = None)
```

`program` maps a `cmd` to the paths it reads/writes, the `host:port`s it connects to, the
bytes it emits, and whether its output derives from fluid input. The default program is a
no-op child that touches nothing.

### `NoJail`

`class NoJail(Jail)` — passthrough that runs out-of-process and propagates taint but
enforces no folder/net scope. The explicit opt-out for code provably not fluid-reachable;
never the default. `kind == "nojail"`.

### `BwrapJail`

`class BwrapJail(_RealJail)` — Linux backend: `bwrap` + seccomp + Landlock. `kind ==
"bwrap"`. `available()` is `True` on Linux when the `bwrap` binary is present.

### `SeatbeltJail`

`class SeatbeltJail(_RealJail)` — macOS backend: `sandbox-exec` / Seatbelt profile.
`kind == "seatbelt"`. `available()` is `True` on darwin when `sandbox-exec` is present.
Extra method `profile(allow_paths, allow_net) -> str` renders the SBPL profile (also used
by tests).

### `select_jail`

```python
def select_jail(policy: SandboxPolicy | None = None) -> Jail
```

OS-sniffing factory. `policy.kind` pins a backend; `None` sniffs (Linux → `BwrapJail`,
macOS → `SeatbeltJail`). Raises `UnsupportedPlatformError` where no real backend exists.

### `registry_descriptors`

```python
def registry_descriptors(
    registry: TypeRegistry = default_registry,
) -> list[dict[str, object]]
```

Serialize a registry's record types to JSON descriptors for the child. Primitives are
nominal and travel implicitly.

### `rehydrate_registry`

```python
def rehydrate_registry(
    descriptors: Sequence[Mapping[str, object]],
    registry: TypeRegistry | None = None,
) -> TypeRegistry
```

Reconstruct a `TypeRegistry` in the child from serialized descriptors (defaults to
`default_registry`), so structural compatibility behaves identically to the parent.

### `emit_denials`

```python
def emit_denials(
    store: Store,
    result: JailResult,
    *,
    run_id: str,
    node_id: str | None = None,
    org_id: str = "local",
    pipeline: str | None = None,
    ts: float = 0.0,
) -> list[Emission]
```

Write one `JAIL_VIOLATION` emission per `Denial` to the ledger; each carries `attempt`
and `severity` attrs and is `tainted=True`. Returns the emissions written.

### `EgressDenied`

`class EgressDenied(RuntimeError)` — raised when host-side code attempts egress to a
non-allowlisted host.

### `EgressBroker`

`class EgressBroker` — mediates network egress against a capability allow-list.

```python
EgressBroker(allow: Iterable[str] = ())
```

| Method | Signature | Behaviour |
| --- | --- | --- |
| `permitted` | `(host: str) -> bool` | `True` if `host` is on the allow-list. |
| `guard` | `(host: str) -> None` | Raises `EgressDenied` if `host` is not permitted. |

### `run_out_of_process`

```python
def run_out_of_process(
    func: Callable[..., R],
    *args: object,
    timeout: float = 30.0,
) -> R
```

Execute `func` (which must be importable/picklable) in a separate process and return its
result, so host-side code never shares the engine's process memory or credentials.

---

## Example

A `FakeJail` run that allows one folder, catches a folder escape, refuses a fluid
allow-path, and shows taint propagating out of a network-touching child. Pure and
in-process — no real sandbox needed.

```python
from crawfish.jail import FakeJail, JailPath, PathMode, StaticOnlyError
from crawfish.jail import _Probe          # the test-injected child description
from crawfish.core.types import Flow

# 1. A jailed child reads inside its allowed folder, then tries to escape to /etc/shadow.
def program(cmd):
    return _Probe(reads=["/work/in.txt", "/etc/shadow"])

jail = FakeJail(program=program)                       # in-process, real policy
allowed = JailPath(path="/work", mode=PathMode.RO)     # STATIC by default
res = jail.run(["node-code"], allow_paths=[allowed])

print("backend     :", jail.kind)
print("exit_code   :", res.exit_code)                  # nonzero: the child escaped
for d in res.denied:
    print("denial      :", d.kind.value, d.attempt, d.severity)

# 2. allow_paths is STATIC-only: a FLUID path can never widen the jail.
fluid = JailPath(path="/secrets", mode=PathMode.RW, flow=Flow.FLUID)
try:
    jail.run(["x"], allow_paths=[fluid])
except StaticOnlyError:
    print("static-only : StaticOnlyError raised for FLUID allow_path")

# 3. Taint boundary: a child that touched the network re-tags its output fluid.
def net(cmd):
    return _Probe(connects=["evil.example:443"])
out = FakeJail(program=net).run(["x"], allow_net=False)
print("out_taint   :", sorted(out.out_taint))
print("net denial  :", out.denied[0].kind.value)
```

??? success "▶ Output"

    ```text
    backend     : fake
    exit_code   : 1
    denial      : folder_escape /etc/shadow high
    static-only : StaticOnlyError raised for FLUID allow_path
    out_taint   : ['fluid']
    net denial  : undeclared_egress
    ```
