"""CRA-110 acceptance: metrics, rubrics, benchmarks, regression detection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from crawfish.batch import Task
from crawfish.core.context import RunContext
from crawfish.definition import Definition
from crawfish.metrics import (
    Benchmark,
    Rubric,
    compare,
    confidence_threshold,
    field_present,
    is_nonempty,
    is_regression,
    output_number,
)
from crawfish.output import Output
from crawfish.runtime import MockRuntime
from crawfish.runtime.base import RunRequest
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _out(value: object) -> Output[object]:
    return Output(output_schema=[], value=value, produced_by="r")


# -- starter metrics --------------------------------------------------------
def test_output_number_from_field_and_string() -> None:
    assert output_number(field="score").evaluate(_out({"score": 0.9, "text": "ok"})) == 0.9
    assert output_number().evaluate(_out("rated 7 of 10")) == 7.0
    assert output_number(default=-1.0).evaluate(_out("no digits here")) == -1.0


def test_field_present() -> None:
    metric = field_present("text")
    assert metric.evaluate(_out({"score": 0.9, "text": "ok"})) == 1.0
    assert metric.evaluate(_out({"score": 0.9})) == 0.0
    assert metric.evaluate(_out({"text": None})) == 0.0


def test_is_nonempty() -> None:
    assert is_nonempty().evaluate(_out("ok")) == 1.0
    assert is_nonempty().evaluate(_out("   ")) == 0.0
    assert is_nonempty().evaluate(_out([])) == 0.0
    assert is_nonempty(field="text").evaluate(_out({"text": "hi"})) == 1.0
    assert is_nonempty(field="text").evaluate(_out({"text": ""})) == 0.0


def test_confidence_threshold() -> None:
    metric = confidence_threshold("score", 0.8)
    assert metric.evaluate(_out({"score": 0.9})) == 1.0
    assert metric.evaluate(_out({"score": 0.5})) == 0.0
    assert metric.evaluate(_out({})) == 0.0


def test_rubric_scores_one_float_per_metric() -> None:
    rubric = Rubric(
        [
            output_number(field="score"),
            field_present("text"),
            is_nonempty(field="text"),
            confidence_threshold("score", 0.8),
        ]
    )
    scores = rubric.score(_out({"score": 0.9, "text": "ok"}))
    assert set(scores) == {m.name for m in rubric.metrics}
    assert len(scores) == 4
    assert all(isinstance(v, float) for v in scores.values())
    assert scores[output_number(field="score").name] == 0.9


# -- benchmark over a fixed task set ----------------------------------------
def _minimal(tmp_path: Path) -> Definition:
    dest = tmp_path / "minimal"
    shutil.copytree(FIXTURES / "minimal", dest, dirs_exist_ok=True)
    return Definition.from_package(str(dest))


def _json_runtime(payload: dict[str, object]) -> MockRuntime:
    def responder(_request: RunRequest) -> str:
        return json.dumps(payload)

    return MockRuntime(responder)


def _rubric() -> Rubric:
    return Rubric(
        [
            output_number(field="score"),
            is_nonempty(field="text"),
            confidence_threshold("score", 0.8),
        ]
    )


async def test_benchmark_aggregates_per_metric(tmp_path: Path) -> None:
    definition = _minimal(tmp_path)
    tasks = [Task(description="a"), Task(description="b"), Task(description="c")]
    benchmark = Benchmark(_rubric(), tasks)
    ctx = RunContext(store=SqliteStore())

    scores = await benchmark.run(definition, ctx, _json_runtime({"score": 0.9, "text": "ok"}))

    assert set(scores) == {m.name for m in _rubric().metrics}
    assert scores[output_number(field="score").name] == 0.9  # mean across 3 tasks
    assert scores[is_nonempty(field="text").name] == 1.0
    assert scores[confidence_threshold("score", 0.8).name] == 1.0


# -- the improvement loop ---------------------------------------------------
async def test_two_versions_produce_comparable_ordered_scores(tmp_path: Path) -> None:
    definition = _minimal(tmp_path)
    tasks = [Task(description="a"), Task(description="b")]
    rubric = _rubric()

    baseline = await Benchmark(rubric, tasks).run(
        definition, RunContext(store=SqliteStore()), _json_runtime({"score": 0.9, "text": "ok"})
    )
    candidate = await Benchmark(rubric, tasks).run(
        definition, RunContext(store=SqliteStore()), _json_runtime({"score": 0.4, "text": ""})
    )

    deltas = compare(baseline, candidate)
    assert deltas[output_number(field="score").name] < 0  # candidate scored lower
    assert is_regression(baseline, candidate)


def test_no_regression_when_candidate_improves_or_holds() -> None:
    baseline = {"score": 0.5, "present": 1.0}
    better = {"score": 0.9, "present": 1.0}
    assert not is_regression(baseline, better)
    assert is_regression(baseline, {"score": 0.49, "present": 1.0})
    # tolerance absorbs small noise
    assert not is_regression(baseline, {"score": 0.49, "present": 1.0}, tolerance=0.05)


def test_compare_handles_unaligned_vectors() -> None:
    deltas = compare({"a": 1.0}, {"b": 2.0})
    assert deltas == {"a": -1.0, "b": 2.0}
