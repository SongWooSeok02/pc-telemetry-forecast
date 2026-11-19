"""Evaluate persistence and trailing-mean baselines without temporal leakage.

Input CSV columns: machine_id,sensor_id,observed_at,value,quality,workload.
Timestamps must be UTC ISO-8601 and observations are aligned to one-second ticks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

INPUT_SECONDS = 300
HORIZON_SECONDS = 60
BASELINES = ("persistence", "moving_average")
VALID_WORKLOADS = {"idle", "high_load", "transition"}


@dataclass(frozen=True)
class Observation:
    value: float | None
    workload: str


@dataclass(frozen=True)
class Example:
    anchor: datetime
    target_time: datetime
    actual: float
    persistence: float
    moving_average: float
    workload: str


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be UTC: {value}")
    if parsed.microsecond:
        raise ValueError(f"timestamp must align to the one-second cadence: {value}")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def read_input(
    path: Path, machine_id: str | None = None, sensor_id: str | None = None
) -> tuple[dict[tuple[str, str], dict[datetime, Observation]], str, int]:
    digest = hashlib.sha256()
    groups: dict[tuple[str, str], dict[datetime, Observation]] = defaultdict(dict)
    invalid_rows = 0
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(chunk)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = {"machine_id", "sensor_id", "observed_at", "value", "quality", "workload"}
        if not reader.fieldnames or not expected.issubset(reader.fieldnames):
            raise ValueError(f"CSV must include columns: {', '.join(sorted(expected))}")
        for line_number, row in enumerate(reader, start=2):
            machine = (row.get("machine_id") or "").strip()
            sensor = (row.get("sensor_id") or "").strip()
            if not machine or not sensor:
                raise ValueError(f"line {line_number}: machine_id and sensor_id are required")
            if (machine_id is not None and machine != machine_id) or (sensor_id is not None and sensor != sensor_id):
                continue
            observed_at = parse_utc(row["observed_at"])
            key = (machine, sensor)
            if observed_at in groups[key]:
                raise ValueError(f"line {line_number}: duplicate timestamp for {machine}/{sensor}")
            quality = (row.get("quality") or "").strip().lower()
            raw_value = (row.get("value") or "").strip()
            value: float | None = None
            if quality == "ok" and raw_value:
                try:
                    candidate = float(raw_value)
                    if math.isfinite(candidate):
                        value = candidate
                except ValueError:
                    pass
            else:
                invalid_rows += 1
            if quality == "ok" and value is None:
                invalid_rows += 1
            workload = (row.get("workload") or "").strip().lower()
            if workload not in VALID_WORKLOADS:
                workload = "unlabeled"
            groups[key][observed_at] = Observation(value, workload)
    return groups, digest.hexdigest(), invalid_rows


def chronological_boundaries(
    latest: datetime, train_days: float = 5, validation_days: float = 1, test_days: float = 2
) -> dict[str, tuple[datetime, datetime]]:
    durations = (train_days, validation_days, test_days)
    if any(not math.isfinite(days) or days <= 0 for days in durations):
        raise ValueError("split durations must be finite and positive")
    if latest.tzinfo is None or latest.utcoffset() != timedelta(0):
        raise ValueError("latest timestamp must be UTC")
    test_end = latest + timedelta(seconds=1)
    test_start = test_end - timedelta(days=test_days)
    validation_start = test_start - timedelta(days=validation_days)
    train_start = validation_start - timedelta(days=train_days)
    return {
        "train": (train_start, validation_start),
        "validation": (validation_start, test_start),
        "test": (test_start, test_end),
    }


def build_examples(
    observations: dict[datetime, Observation], split_start: datetime, split_end: datetime
) -> tuple[list[Example], int]:
    """Build 300-second inputs and +60s targets wholly contained in a split."""
    for name, boundary in (("split_start", split_start), ("split_end", split_end)):
        if boundary.tzinfo is None or boundary.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be UTC")
    if split_start >= split_end:
        raise ValueError("split_start must be earlier than split_end")
    if not observations:
        return [], 0
    first, last = min(observations), max(observations)
    earliest_anchor = max(
        first + timedelta(seconds=INPUT_SECONDS - 1),
        split_start + timedelta(seconds=INPUT_SECONDS - 1),
    )
    # Telemetry is sampled on whole-second UTC ticks. Ceiling a fractional
    # split boundary keeps candidate anchors on that same grid.
    first_anchor = earliest_anchor.replace(microsecond=0)
    if earliest_anchor.microsecond:
        first_anchor += timedelta(seconds=1)
    latest_anchor = min(
        last - timedelta(seconds=HORIZON_SECONDS),
        split_end - timedelta(seconds=HORIZON_SECONDS),
    )
    last_anchor = latest_anchor.replace(microsecond=0)
    if last_anchor + timedelta(seconds=HORIZON_SECONDS) >= split_end:
        last_anchor -= timedelta(seconds=1)
    if first_anchor > last_anchor:
        return [], 0
    examples: list[Example] = []
    possible = 0
    anchor = first_anchor
    second = timedelta(seconds=1)
    while anchor <= last_anchor:
        target_time = anchor + timedelta(seconds=HORIZON_SECONDS)
        if target_time >= split_end:
            break
        possible += 1
        window = [observations.get(anchor - timedelta(seconds=offset)) for offset in range(INPUT_SECONDS - 1, -1, -1)]
        target = observations.get(target_time)
        if target and target.value is not None and all(point is not None and point.value is not None for point in window):
            values = [point.value for point in window if point and point.value is not None]
            examples.append(Example(
                anchor=anchor,
                target_time=target_time,
                actual=target.value,
                persistence=values[-1],
                moving_average=sum(values) / INPUT_SECONDS,
                workload=target.workload,
            ))
        anchor += second
    return examples, possible


def mae(examples: Iterable[Example], model: str) -> float | None:
    values = list(examples)
    if not values:
        return None
    return sum(abs(example.actual - getattr(example, model)) for example in values) / len(values)


def metrics(examples: list[Example], possible: int, model: str) -> dict[str, object]:
    groups: dict[str, list[Example]] = defaultdict(list)
    for example in examples:
        groups[example.workload].append(example)
    by_workload = {
        workload: {"sample_count": len(items), "mae": mae(items, model)}
        for workload, items in sorted(groups.items())
    }
    return {
        "sample_count": len(examples),
        "missing_rate": 1 - len(examples) / possible if possible else None,
        "mae": mae(examples, model),
        "by_workload": by_workload,
    }


def evaluate(
    groups: dict[tuple[str, str], dict[datetime, Observation]],
    input_sha256: str,
    invalid_rows: int,
    train_days: float = 5,
    validation_days: float = 1,
    test_days: float = 2,
) -> dict[str, object]:
    all_times = [timestamp for timeline in groups.values() for timestamp in timeline]
    if not all_times:
        raise ValueError("input has no observations")
    latest = max(all_times)
    boundaries = chronological_boundaries(latest, train_days, validation_days, test_days)
    results: dict[str, object] = {}
    for (machine, sensor), timeline in sorted(groups.items()):
        split_examples: dict[str, list[Example]] = {}
        split_possible: dict[str, int] = {}
        for split_name, (start, end) in boundaries.items():
            split_examples[split_name], split_possible[split_name] = build_examples(timeline, start, end)
        validation = split_examples["validation"]
        if not validation:
            results[f"{machine}/{sensor}"] = {"status": "insufficient_validation_data", "splits": {
                name: {"possible_count": split_possible[name], "valid_count": len(examples)}
                for name, examples in split_examples.items()
            }}
            continue
        validation_scores = {model: mae(validation, model) for model in BASELINES}
        selected = min(BASELINES, key=lambda model: (validation_scores[model], BASELINES.index(model)))
        split_results: dict[str, object] = {}
        for split_name, examples in split_examples.items():
            split_results[split_name] = {
                "possible_count": split_possible[split_name],
                "models": {model: metrics(examples, split_possible[split_name], model) for model in BASELINES},
                "selected_baseline": selected,
            }
        test_score = mae(split_examples["test"], selected)
        test_baseline_score = mae(split_examples["test"], "persistence" if selected == "moving_average" else "moving_average")
        improvement = None
        if test_score is not None and test_baseline_score is not None and test_baseline_score > 0:
            improvement = (test_baseline_score - test_score) / test_baseline_score * 100
        split_results["test"]["selected_baseline_improvement_vs_other_percent"] = improvement
        results[f"{machine}/{sensor}"] = {
            "status": "evaluated" if split_examples["test"] else "insufficient_test_data",
            "selected_baseline": selected,
            "validation_selection_mae": validation_scores,
            "splits": split_results,
        }
    return {
        "schema_version": "1.0",
        "pipeline_version": "s2-07b.2",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_sha256": input_sha256,
        "config": {
            "input_seconds": INPUT_SECONDS,
            "horizon_seconds": HORIZON_SECONDS,
            "cadence_seconds": 1,
            "train_days": train_days,
            "validation_days": validation_days,
            "test_days": test_days,
            "selection_split": "validation",
            "test_used_for_selection_or_tuning": False,
        },
        "boundaries_utc": {
            name: {"start_inclusive": start.isoformat().replace("+00:00", "Z"),
                   "end_exclusive": end.isoformat().replace("+00:00", "Z")}
            for name, (start, end) in boundaries.items()
        },
        "invalid_quality_rows": invalid_rows,
        "targets": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTC long-form telemetry CSV")
    parser.add_argument("--output", type=Path, required=True, help="JSON evaluation report path")
    parser.add_argument("--machine-id", required=True, help="evaluate one PC to bound memory use")
    parser.add_argument("--sensor-id", required=True, help="evaluate one target sensor")
    parser.add_argument("--train-days", type=float, default=5)
    parser.add_argument("--validation-days", type=float, default=1)
    parser.add_argument("--test-days", type=float, default=2)
    args = parser.parse_args()
    groups, digest, invalid_rows = read_input(args.input, args.machine_id, args.sensor_id)
    report = evaluate(groups, digest, invalid_rows, args.train_days, args.validation_days, args.test_days)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "targets": len(report["targets"]),
                      "input_sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
