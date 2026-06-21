# Secrets & consent

How credentials are held without ever leaking, and how a package gets permission to
touch them. Secrets live **by reference** (an env-var name), are resolved least-privilege,
and are stripped out of anything written or logged. Before a package may use them, its
declared needs are surfaced for explicit consent and recorded as a grant. These live in
`crawfish.secrets`.

**Symbols on this page:** `resolve_secret` · `load_env` · `SecretManager` ·
`ScrubbingStore` · `redact` · `read_capabilities` · `Capabilities` · `ConsentRequest` ·
`ConsentDecider` · `AutoConsent` · `DenyConsent` · `CallbackConsent` · `GrantManifest` ·
`ConsentDeclined` · `consent_install` · `GRANT_RECORD_KIND`

> The *runtime* half of `crawfish.secrets` — the broker that injects a credential at the
> network boundary so a jailed agent never sees its value (`Grant`, `SecretBroker`,
> `LeaseHandle`, leases, egress) — is documented separately in
> [Secret broker](secret-broker.md). This page covers resolution, scrubbing, and the
> install-time consent gate.

---

## Core

A **secret** is a credential — an API key, a token. Crawfish never stores the value of
one. Instead a node names the *reference*: the name of an environment variable (e.g.
`"GITHUB_TOKEN"`) that holds the value. The value lives only in the process environment
or a gitignored `.env` file; the framework passes around the name, and looks the value up
at the last possible moment.

- **`resolve_secret`** turns one reference name into its value (or `None` if it is unset).
- **`load_env`** parses a `.env` file into a `name → value` map.
- **`SecretManager`** holds that map and enforces *least privilege*: each node is given
  only the secrets it explicitly **declared** it needs, never the whole environment.

Even held by reference, a value can still leak by being *written down* — into a saved
transcript, a log line, a stored event. Two pieces guard that exit:

- **`redact`** takes a string and replaces any known secret value (plus common
  credential/PII patterns like an `sk-…` key or an email address) with a fixed marker.
- **`ScrubbingStore`** wraps the persistence layer (the **Store** — Crawfish's database
  seam) and runs `redact` over everything on the way *in*, so the saved ledger never
  contains a raw credential.

Holding secrets safely is half the job. The other half is **consent**: deciding that a
package is *allowed* to touch a given secret or reach a given network host at all.

- A package **declares** what it needs in its `crawfish.toml`. **`read_capabilities`**
  reads that declaration into a **`Capabilities`** object — the list of secret references
  and egress (outbound network) hosts it wants.
- At install time those declared needs are turned into a **`ConsentRequest`** — a
  read-only summary shown for approval (secrets by *reference name*, never value).
- A **`ConsentDecider`** says yes or no. Three are built in: **`AutoConsent`** (always
  yes — for an explicit `--yes` install), **`DenyConsent`** (always no — the fail-closed
  default when nobody is there to ask), and **`CallbackConsent`** (wraps your own
  yes/no function, e.g. a stdin prompt).
- On approval, **`consent_install`** records a **grant** through the **`GrantManifest`**
  and returns it. On refusal it records nothing and raises **`ConsentDeclined`**, so the
  package stays *fail-closed* — it can use nothing it was not granted.

---

## Ramps up

### By reference, never by value

The load-bearing rule (see the [security spine](../architecture/SECURITY.md)): a
credential value never reaches stored config, an output, a log, or the model prompt. A
node declares the env-var *name* it needs; `SecretManager.for_node` resolves only those
names, only for that node. This is the embryonic capability manifest — least privilege
from day one. The known v1 tradeoff is that a local command runtime can still read `.env`
inside its sandbox; that gap is closed by the broker's egress-mediated injection, covered
in [Secret broker](secret-broker.md).

### Scrub before the write, not after

`ScrubbingStore` redacts on the *write* path — `put_record`, `kv_set`, and `append_event`
pass their payloads through `redact_obj` (the recursive form of `redact`) before handing
them to the inner Store. Read paths (`get_record`, `events`, …) are pass-through. The
consequence: a value that was never written can never be read back, and the persisted
event ledger is clean by construction rather than by a later sweep. It implements the same
Store protocol it wraps, so it drops in anywhere a Store is expected.

`redact` matches two things: exact known secret values (passed in), and a fixed set of
credential/PII regexes — `sk-…` and `ghp_…`/`xox…` tokens, `Bearer …` headers, and email
addresses. Both are replaced with the literal marker `***REDACTED***`. Pattern matching
means a credential is scrubbed *even if* it was never registered as a known value.

### Consent is explicit and static

The consent surface shows only the **statically declared** capabilities — a per-item
*fluid* value (untrusted session data) can never appear here and can never grant a
capability. A detached or non-interactive install has no human to ask, so the default
decider is `DenyConsent`: nothing self-approves silently. This mirrors the broker's
fail-closed approval queue default. Approval is recorded as a `Grant` under the Store
record kind `GRANT_RECORD_KIND` (`"capability_grant"`), one grant per `(org_id, package)`
— re-consenting a package overwrites its prior grant. On decline, **no** record is
written.

### Why `ConsentDecider` is a protocol

`ConsentDecider` is a `runtime_checkable` `Protocol` — an injectable seam — so tests never
touch real stdin and a CLI can plug in a stdin-prompt callback via `CallbackConsent`. The
three concrete deciders cover the install modes: interactive (`CallbackConsent`), explicit
non-interactive yes (`AutoConsent`), and fail-closed default (`DenyConsent`).

---

## API reference

### `resolve_secret`

```python
def resolve_secret(ref: str | None, env: Mapping[str, str] | None = None) -> str | None
```

Resolve a secret reference (an env-var name) to its value. Returns `None` if `ref` is
falsy. If `env` is given, looks the name up there; otherwise reads `os.environ`.

### `load_env`

```python
def load_env(path: str | Path = ".env") -> dict[str, str]
```

Parse a gitignored `.env` file of `KEY=VALUE` lines into a dict. Blank lines, comments
(`#`), and lines without `=` are skipped; surrounding quotes are stripped from values.
A missing file yields `{}`. Values are never logged.

### `SecretManager`

`class SecretManager` — maps nodes to the secrets they declare and resolves them
least-privilege.

| Member | Signature | Notes |
| --- | --- | --- |
| `__init__` | `(env: Mapping[str, str] \| None = None)` | Uses `env` if given, else `load_env()`. |
| `declare` | `(node_id: str, refs: Iterable[str]) -> None` | Record the secret references a node needs (falsy refs ignored). |
| `for_node` | `(node_id: str) -> dict[str, str]` | Only the secrets this node declared **and** that exist in the env. |
| `values` | `property -> list[str]` | All known non-empty secret values (for wiring a `ScrubbingStore`). |

### `redact`

```python
def redact(text: str, secrets: Iterable[str] = ()) -> str
```

Replace each non-empty value in `secrets`, and each built-in credential/PII pattern, with
the marker `***REDACTED***`. Built-in patterns: `sk-…` keys, `ghp_…` tokens, `xox[baprs]-…`
Slack tokens, `Bearer …` headers, and email addresses.

### `ScrubbingStore`

`class ScrubbingStore` — a `Store` wrapper that redacts secrets/PII before any write.

| Member | Signature | Behaviour |
| --- | --- | --- |
| `__init__` | `(inner: Store, secrets: Iterable[str] = ())` | Wrap `inner`; `secrets` are the known values to redact. |
| `put_record` | `(kind, id, data, *, org_id="local") -> None` | Redacts `data` (recursively) before writing. |
| `kv_set` | `(namespace, key, value, *, org_id="local") -> None` | Redacts `value` before writing. |
| `append_event` | `(run_id, event, *, org_id="local") -> None` | Redacts `event` before writing. |
| `get_record` / `list_records` / `delete_record` / `kv_get` / `claim_idempotency` / `events` / `close` | — | Pass-through to `inner` (read/admin paths are not redacted). |

### `read_capabilities`

```python
def read_capabilities(project_dir: str | Path) -> Capabilities
```

Read a package's declared capabilities from `crawfish.toml`'s `[capabilities]` table
(`secrets` and `egress` lists). Returns an empty `Capabilities` if the file is absent.

### `Capabilities`

`class Capabilities` — what a package/unit declares it needs (the consent surface).

| Member | Signature | Notes |
| --- | --- | --- |
| `__init__` | `(*, secrets: list[str] \| None = None, egress: list[str] \| None = None)` | Default each to `[]`. |
| `secrets` | `list[str]` | Declared secret references (env-var names). |
| `egress` | `list[str]` | Declared egress hosts. |
| `summary` | `() -> str` | Human-readable line, e.g. `"secrets: A, B; network egress: api.x"`, or `"no special capabilities"`. |

### `ConsentRequest`

`@dataclass(frozen=True) class ConsentRequest` — the static consent surface shown to a
decider. Carries references only, never a value.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `package` | `str` | — (required) | Package being installed. |
| `secrets` | `tuple[str, ...]` | `()` | Declared secret references. |
| `egress` | `tuple[str, ...]` | `()` | Declared egress hosts. |

| Method | Signature | Notes |
| --- | --- | --- |
| `from_capabilities` | `classmethod (package: str, caps: Capabilities) -> ConsentRequest` | Build from a `Capabilities`. |
| `summary` | `() -> str` | References-only summary, e.g. `"secrets (by reference): TOKEN; network egress: api.x"`. |

### `ConsentDecider`

`@runtime_checkable class ConsentDecider(Protocol)` — the injectable consent-decision seam.

```python
def decide(self, request: ConsentRequest) -> bool: ...
```

Returns `True` to grant. Implementations: `AutoConsent`, `DenyConsent`, `CallbackConsent`.

### `AutoConsent`

`class AutoConsent` — `decide` always returns `True`. For an explicit, non-interactive
`--yes` install only.

### `DenyConsent`

`class DenyConsent` — `decide` always returns `False`. The fail-closed default for a
detached/non-interactive install: with no human to consent, the install raises
`ConsentDeclined` rather than self-approve.

### `CallbackConsent`

`class CallbackConsent` — wraps a `Callable[[ConsentRequest], bool]` as a decider.

| Member | Signature | Notes |
| --- | --- | --- |
| `__init__` | `(fn: Callable[[ConsentRequest], bool])` | The yes/no callback (a CLI prompt, or a test lambda). |
| `decide` | `(request: ConsentRequest) -> bool` | Returns `bool(fn(request))`. |

### `GrantManifest`

`class GrantManifest` — a Store-backed, queryable manifest of consented capability grants.
One grant per `(org_id, package)`, persisted under `GRANT_RECORD_KIND`.

| Member | Signature | Notes |
| --- | --- | --- |
| `__init__` | `(store: Store, *, org_id: str = "local")` | Bind to a Store and org. |
| `save` | `(grant: Grant) -> None` | Persist (or overwrite) the grant for `grant.package`. |
| `lookup` | `(package: str) -> Grant \| None` | The consented grant for `package`, or `None`. |
| `list` | `() -> list[Grant]` | Every grant in this org (audit/consent surface). |
| `revoke` | `(package: str) -> None` | Remove a package's grant (fail-closed afterward). |

`Grant` itself is documented in [Secret broker](secret-broker.md).

### `ConsentDeclined`

`class ConsentDeclined(RuntimeError)` — raised when an install is attempted but consent
was not explicitly granted. No grant is written, so the package can lease nothing.

### `consent_install`

```python
def consent_install(
    package: str,
    caps: Capabilities,
    *,
    store: Store,
    decider: ConsentDecider | None = None,
    org_id: str = "local",
    now: float | None = None,
) -> Grant
```

The install-time consent gate. Steps: (1) build a static `ConsentRequest` from `caps`;
(2) ask `decider` (default `DenyConsent` — fail-closed); (3) on approval, mint a `Grant`
(stamped `granted_at = now`, or `time.time()` if `now` is `None`), persist it via
`GrantManifest`, and return it; (4) on decline, write nothing and raise `ConsentDeclined`.

### `GRANT_RECORD_KIND`

`GRANT_RECORD_KIND: str = "capability_grant"` — the Store record `kind` under which
consented grants are persisted. One grant per `(org_id, package)`; re-consenting
overwrites.

---

## Example

Redacting a string that contains a secret value, then running one `ConsentRequest`
through `AutoConsent` (granted) and `DenyConsent` (refused → `ConsentDeclined`, caught).
All pure and in-memory — no real secrets, no network.

```python
from crawfish.secrets import (
    redact, Capabilities, ConsentRequest,
    AutoConsent, DenyConsent, ConsentDecider, ConsentDeclined,
)

# A log line that accidentally embeds a token. redact() masks it.
SECRET = "ghp_0123456789abcdefghij0123456789abcd"
line = f"calling GitHub with token={SECRET} for user a@b.com"
print(redact(line, secrets=[SECRET]))

# The static consent surface a decider inspects (references only, never a value).
caps = Capabilities(secrets=["GITHUB_TOKEN"], egress=["api.github.com"])
request = ConsentRequest.from_capabilities("triage-bot", caps)
print(request.summary())


def run(decider: ConsentDecider) -> str:
    # Mirrors consent_install's gate without needing a Store.
    if not decider.decide(request):
        raise ConsentDeclined(f"declined: cannot lease {request.summary()}")
    return "granted"


print(run(AutoConsent()))         # always yes
try:
    run(DenyConsent())            # always no -> raises
except ConsentDeclined as exc:
    print("declined:", "GITHUB_TOKEN" in str(exc))
```

??? success "▶ Output"

    ```text
    calling GitHub with token=***REDACTED*** for user ***REDACTED***
    secrets (by reference): GITHUB_TOKEN; network egress: api.github.com
    granted
    declined: True
    ```
