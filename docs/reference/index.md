# Reference

The explained reference. Every public symbol in `crawfish.__all__` is covered here
with worked examples — this is the layer the [flat API dump](../guide/api-reference.md)
links into, not a replacement for it.

## How to read a page

Each page covers one cluster of related symbols and is built in three tiers, so you
stop at the depth you need:

1. **Core** — plain English, every term defined before use. What it is, why it exists.
2. **Ramps up** — mechanics, invariants, edge cases, and the design rationale (with the
   relevant ADR).
3. **API reference** — exact signature, fields, and members for each symbol, verified
   against the source.

Every page carries at least one runnable example with a collapsible **▶ Output** block.
Examples are deterministic: they run on pure functions or
[`MockRuntime`](runtimes.md#mockruntime), never a live model, so the shown output never
drifts.

## The map

Start at the substrate and ramp outward.

| Layer | Pages |
| --- | --- |
| **Substrate** | [Core types](core-types.md) · [Type system](type-system.md) · [Versioning & stability](versioning-and-stability.md) · [Context & budgets](context-and-budgets.md) |
| **Wiring & data** | [Output & wiring](output-and-wiring.md) · [Validation](validation.md) · [Context carry](context-carry.md) |
| **Authoring agents** | [Definition](definition.md) · [Runtimes](runtimes.md) · [Providers](providers.md) |
| **Pipeline nodes** | [Source & filter](nodes-source-filter.md) · [Aggregator](nodes-aggregator.md) · [Router & sink](nodes-router-sink.md) |
| **Running work** | [Run & engine](run-and-engine.md) · [Batch & execution](batch-and-execution.md) · [Persistence](persistence.md) |
| **Observe & control** | [Emission, inspector & visualize](emission-inspector-visualize.md) · [Anomaly](anomaly.md) · [Observer](observer.md) · [Cost, routing & cache](cost-routing-cache.md) |
| **Measure & improve** | [Metrics](metrics.md) · [Evals](evals.md) · [Tuner & learning](tuner-and-learning.md) |
| **Operate** | [Operate](operate.md) · [Authoring](authoring.md) |
| **Security spine** | [Secrets & consent](secrets-and-consent.md) · [Secret broker](secret-broker.md) · [Sandbox & jail](sandbox-and-jail.md) |
| **Tooling** | [Testing](testing.md) · [Claude Code export](claude-code-export.md) |
