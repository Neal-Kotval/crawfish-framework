# Authoring benchmark — base Claude Code vs craw code

Does **craw code** (authoring skills + the `craw code` CLI/jail/gate) help a Claude Code agent
*write* a safe, correct Crawfish project? This is the **authoring-layer** complement to the
operate-layer benchmark in [`../`](../README.md) (which measures the *runtime*).

Full design, metrics, pilot results, and a features roadmap:
[`docs/dev/craw-code/03-BENCHMARKS.md`](../../docs/dev/craw-code/03-BENCHMARKS.md).

> ⚠️ Makes live `claude -p` calls — costs money, non-deterministic, **not** in the pytest suite.

## Run

```bash
CRAW_BENCH_K=3 bash bench/authoring/author_bench.sh   # K runs per task×arm; prints cost/speed table
python3 bench/authoring/evaluate.py bench/authoring/.out   # score outputs vs the real pipeline
```

Two arms, identical task text per pair:
- **base** — task + a minimal API hint.
- **craw** — the same + `craw_suffix.txt` (read the spine/authoring skills; self-check with `craw code describe`).

## Layout
- `tasks/*.txt` — one-shot authoring tasks (add your own; same text goes to both arms).
- `craw_suffix.txt` — the craw-arm treatment (skills + self-check; authors only under `./definitions/`).
- `author_bench.sh` — runs the matrix, captures `total_cost_usd`/`duration_ms`/`num_turns`/tokens.
- `evaluate.py` — scores `compiles` / `gate` / `sink_target_safe` against `load_definition_jailed` + `assert_build_safe`.
- `RESULTS.md` — the latest pilot results.
- `.out/` — run outputs (gitignored).
