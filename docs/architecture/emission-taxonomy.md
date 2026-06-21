# Emission taxonomy (frozen)

The canonical contract for `crawfish.emission.Emission` — the one typed signal every
producer emits onto the append-only ledger and every consumer (dashboard CRA-181, anomaly
engine CRA-190, inspector) reads. Frozen in CRA-184; see
[ADR 0013](decisions/0013-emission-taxonomy-and-inline-output-value.md).

`EmissionKind` is **closed**. Adding a kind or changing a kind's required attrs is a
contract change: bump `EMISSION_SCHEMA_VERSION` and extend `REQUIRED_ATTRS` in the same PR,
and update this table.

## Envelope (all kinds)

| Field | Type | Notes |
| -- | -- | -- |
| `id` | `str` | unique per emission |
| `schema_version` | `int` | `EMISSION_SCHEMA_VERSION` (currently `1`) |
| `kind` | `EmissionKind` | one of the table below |
| `run_id` | `str` | the run this belongs to |
| `org_id` | `str` | tenancy key (default `"local"`) |
| `pipeline` | `str \| None` | pipeline id, when applicable |
| `node_id` | `str \| None` | emitting agent/node, when applicable |
| `ts` | `float` | epoch seconds; emitters stamp it |
| `attrs` | `dict[str, JSONValue]` | kind-specific payload (see below) |
| `tainted` | `bool` | `True` if any `attrs` value derives from fluid/untrusted input |

## Kinds and required `attrs`

| Kind | Required `attrs` | Emitted by | Meaning |
| -- | -- | -- | -- |
| `run_start` | `runtime` | runtime / engine | a pipeline/agent run began |
| `run_finish` | `status` | runtime / engine | a run reached a terminal state |
| `model` | `model`, `cost_usd` | AgentRuntime (CRA-171/173) | one model turn |
| `tool` | `tool` | runtime tool channel | a tool/MCP call — **result is untrusted → `tainted`** |
| `sink` | `target`, `committed` | Sink nodes | a consequential side effect attempted/committed |
| `compaction` | `strategy` | context strategies (CRA-174) | context was compacted/summarized |
| `observer` | `kind`, `severity` | observer surface (CRA-181/190) | an `ObserverEvent` crossed into the stream |
| `metric` | `metric`, `value` | evals/metrics (CRA-175) | a measured `Metric`/`Rubric` value |
| `secret_lease` | `ref`, `node_id` | broker (CRA-178) | the broker leased a secret to a node |
| `jail_violation` | `attempt`, `severity` | sandbox (CRA-179) | the jail blocked an escape attempt |

Consumers may rely on the required keys being present; producers may add extra keys.
`Emission.missing_attrs()` / `Emission.is_valid()` check a payload against this schema (a
pure, deterministic check used by the emit path and the CRA-185 conformance suite).

## Security notes

- `tool`, `secret_lease`, and `jail_violation` emissions are security-relevant. Tool/MCP
  results re-enter the model as content and are untrusted — set `tainted=True`.
- Secret **values** never appear in `attrs` — `secret_lease` carries the *reference*
  (`ref`) only, and the ledger is written through `ScrubbingStore` (secrets/PII redacted).
- `tainted` must survive serialization to/from the ledger (CRA-171) so downstream
  consumers cannot launder untrusted content into a trusted decision.
