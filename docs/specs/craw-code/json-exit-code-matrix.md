# `craw` --json / exit-code coverage matrix (CRA-243)

`craw code` drives a project by parsing `craw … --json` over Bash, so every agent-facing
verb must (1) emit a versioned `craw.<cmd>.v<N>` payload under `--json` through the single
shared emitter (`crawfish.code.emit_json` / the optimization plane's `_opt_print`), and
(2) return a **meaningful exit code** from the uniform table below. Errors are emitted as
the `craw.error.v1` envelope (CRA-270) on stderr — never a raw traceback.

This matrix is the deliverable of the CRA-243 audit and is snapshot-tested
(`test_cli_json_coverage.py`): a regression that changes a code without updating this doc
fails CI.

## Exit-code table (uniform across `craw` verbs)

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | ok | success |
| `1` | expected_failure | expected failure (regression gate tripped, consent declined, goal not met) |
| `2` | usage | usage / compile error (bad args, `DefinitionLoadError`, jail `Denial` at parse) |
| `3` | budget_exceeded | a `--budget` / project `[budget]` ceiling halted the run |
| `4` | security_rejection | assembly gate, fluid→static-sink, signing/consent required — **non-retryable** |

## Error envelope (CRA-270)

On error, `--json` mode emits a `craw.error.v1` envelope on **stderr**:

```json
{ "schema": "craw.error.v1",
  "schema_version": {"major": 1, "minor": 0},
  "code": "fluid_to_static_sink",
  "retryable": false,
  "detail": {"component": "pipelines/triage", "slot": "sink.target"},
  "remediation": "A sink target is static-only; bind it from static config, not a fluid input." }
```

`code` is a closed enum; every **security** rejection (`jail_violation`,
`fluid_to_static_sink`, `signing_required`, `consent_required`, `schema_skew`) is
`retryable: false` — an injected agent must not retry past a security gate. `remediation`
is a static string; fluid/tainted input never round-trips into it.

## Verb coverage (`craw code` family + the planes that feed it)

| Verb | `--json` | Schema tag | Exit codes |
| --- | --- | --- | --- |
| `craw code schema` | yes | `craw.code.schema.v1` | `0` |
| `craw code describe` | yes (M1) | `craw.code.describe.v1` | `0`, `2`, `4` |
| `craw code estimate` | yes (M1) | `craw.code.estimate.v1` | `0`, `3` |
| `craw code run` / `sync` | yes (M1) | `craw.code.run.v1` / `…sync.v1` | `0`, `2`, `3`, `4` |
| `craw eval` / `tune` / `refine` / `learn` / `guard` | yes | `craw.<cmd>.v1` | `0`, `1` |
| `craw replay` / `prove` | yes | `craw.replay.v1` / `craw.prove.v1` | `0`, `1` |

The human one-liner mode is unchanged when `--json` is absent — `--json` is purely
additive. The `code/` rows land their schemas in `crawfish.code.SCHEMA_VERSIONS`
(CRA-269); the M1 verbs (`describe`/`estimate`/`run`/`sync`) are filed there ahead of
their implementation so the negotiation map is complete at session start.
