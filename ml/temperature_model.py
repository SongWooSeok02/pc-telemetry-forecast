"""Deterministic per-PC temperature regression, chronological evaluation and fallback."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ml.evaluate_baselines import chronological_boundaries, parse_utc

VERSION = "s3-04.1"
SIGNALS = ("temperature", "power", "utilization", "clock", "fan")
WORKLOADS = ("idle", "high_load", "transition")
INPUT = 300
HORIZON = 60
STRIDE = 60
MIN_COVERAGE = 0.9


@dataclass(frozen=True)
class Sample:
    values: tuple[float | None, ...]
    workload: str
    boot: str
    config: str


@dataclass(frozen=True)
class Example:
    anchor: datetime
    features: tuple[float, ...]
    actual: float
    persistence: float
    moving_average: float
    workload: str


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_samples(path: Path, machine: str, target: str) -> dict[datetime, Sample]:
    required = {"machine_id", "target", "observed_at", "temperature", "temperature_quality",
                "time_quality", "workload", "boot_session_id", "sensor_config_id"}
    samples = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV requires: {sorted(required)}")
        for row in reader:
            if row["machine_id"] != machine or row["target"] != target:
                continue
            stamp = parse_utc(row["observed_at"])
            if stamp in samples:
                raise ValueError("duplicate observation for selected PC/target")
            if not row["boot_session_id"] or not row["sensor_config_id"]:
                raise ValueError("boot_session_id and sensor_config_id are required")
            values = []
            for signal in SIGNALS:
                value = None
                if row["time_quality"] == "ok" and row.get(f"{signal}_quality") == "ok":
                    try:
                        candidate = float(row.get(signal, ""))
                        if math.isfinite(candidate):
                            value = candidate
                    except (TypeError, ValueError):
                        pass
                values.append(value)
            workload = row["workload"] if row["workload"] in WORKLOADS else "unlabeled"
            samples[stamp] = Sample(tuple(values), workload, row["boot_session_id"], row["sensor_config_id"])
    return samples


def window(samples: dict[datetime, Sample], anchor: datetime, signals: list[str]) -> list[Sample] | None:
    rows = [samples.get(anchor - timedelta(seconds=i)) for i in range(INPUT - 1, -1, -1)]
    if any(row is None for row in rows):
        return None
    indices = [SIGNALS.index(name) for name in signals]
    first = rows[0]
    if any((row.boot, row.config) != (first.boot, first.config) or
           any(row.values[index] is None for index in indices) for row in rows):
        return None
    return rows


def features(rows: list[Sample], signals: list[str]) -> tuple[float, ...]:
    result = []
    for name in signals:
        values = [row.values[SIGNALS.index(name)] for row in rows]
        result.extend((values[-1], sum(values) / INPUT, (values[-1] - values[0]) / (INPUT - 1)))
        if name == "temperature":
            result.extend((min(values), max(values)))
    return tuple(result)


def select_signals(samples: dict[datetime, Sample], start: datetime, end: datetime) -> list[str]:
    # Only training rows determine feature availability; future support cannot leak in.
    expected = (end - start).total_seconds()
    counts = [0] * len(SIGNALS)
    for stamp, row in samples.items():
        if start <= stamp < end:
            for i, value in enumerate(row.values):
                counts[i] += value is not None
    return ["temperature"] + [name for i, name in enumerate(SIGNALS[1:], 1)
                                if counts[i] / expected >= MIN_COVERAGE]


def build_examples(samples: dict[datetime, Sample], start: datetime, end: datetime,
                   signals: list[str]) -> tuple[list[Example], dict]:
    examples = []
    possible = 0
    counts = {name: {"possible": 0, "valid": 0} for name in (*WORKLOADS, "unlabeled")}
    # Fixed UTC grid, independent of input ordering or a fractional split boundary.
    earliest = start + timedelta(seconds=INPUT - 1)
    anchor = datetime.fromtimestamp(math.ceil(earliest.timestamp() / STRIDE) * STRIDE, timezone.utc)
    while anchor + timedelta(seconds=HORIZON) < end:
        possible += 1
        target = samples.get(anchor + timedelta(seconds=HORIZON))
        label = target.workload if target else "unlabeled"
        counts[label]["possible"] += 1
        rows = window(samples, anchor, signals) if target and target.values[0] is not None else None
        if rows and target and target.values[0] is not None and (target.boot, target.config) == (rows[-1].boot, rows[-1].config):
            temperatures = [row.values[0] for row in rows]
            examples.append(Example(anchor, features(rows, signals), target.values[0],
                                    temperatures[-1], sum(temperatures) / INPUT, label))
            counts[label]["valid"] += 1
        anchor += timedelta(seconds=STRIDE)
    seconds = (end - start).total_seconds()
    valid_seconds = sum(start <= stamp < end and row.values[0] is not None for stamp, row in samples.items())
    return examples, {"possible_count": possible, "valid_count": len(examples),
                      "missing_rate": 1 - len(examples) / possible if possible else None,
                      "temperature_coverage": valid_seconds / seconds,
                      "candidate_counts_by_workload": counts}


def fit_ridge(examples: list[Example], alpha: float = 1.0) -> dict:
    if not examples or not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("training samples and positive finite ridge alpha required")
    columns = list(zip(*(example.features for example in examples)))
    means = [sum(values) / len(values) for values in columns]
    scales = [math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)) or 1.0
              for values, mean in zip(columns, means)]
    intercept = sum(example.actual for example in examples) / len(examples)
    n = len(means)
    matrix = [[alpha if i == j else 0.0 for j in range(n)] + [0.0] for i in range(n)]
    for example in examples:
        vector = [(value - mean) / scale for value, mean, scale in zip(example.features, means, scales)]
        for i in range(n):
            matrix[i][-1] += vector[i] * (example.actual - intercept)
            for j in range(n):
                matrix[i][j] += vector[i] * vector[j]
    for i in range(n):
        pivot = max(range(i, n), key=lambda row: abs(matrix[row][i]))
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        divisor = matrix[i][i]
        if abs(divisor) < 1e-12:
            raise ValueError("singular training system")
        matrix[i] = [value / divisor for value in matrix[i]]
        for row in range(n):
            if row != i:
                factor = matrix[row][i]
                matrix[row] = [value - factor * other for value, other in zip(matrix[row], matrix[i])]
    model = {"means": means, "scales": scales, "intercept": intercept,
             "coefficients": [row[-1] for row in matrix], "alpha": alpha}
    if not all(math.isfinite(value) for values in (means, scales, model["coefficients"], [intercept]) for value in values):
        raise ValueError("non-finite fitted model")
    return model


def predict_model(model: dict, vector: tuple[float, ...]) -> float:
    if len(vector) != len(model["coefficients"]):
        raise ValueError("feature dimension mismatch")
    prediction = model["intercept"] + sum(coefficient * (value - mean) / scale
        for value, mean, scale, coefficient in zip(vector, model["means"], model["scales"], model["coefficients"]))
    if not math.isfinite(prediction):
        raise ValueError("non-finite prediction")
    return prediction


def scores(examples: list[Example], predict: Callable[[Example], float], diagnostics: dict) -> dict:
    errors = [(example.workload, abs(example.actual - predict(example))) for example in examples]
    def average(values):
        if not values or not all(math.isfinite(value) for value in values):
            return None
        value = sum(error / len(values) for error in values)
        return value if math.isfinite(value) else None
    by_workload = {}
    for name in WORKLOADS:
        values = [error for workload, error in errors if workload == name]
        count = diagnostics["candidate_counts_by_workload"][name]
        by_workload[name] = {"mae": average(values),
                             "sample_count": len(values),
                             "missing_rate": 1 - count["valid"] / count["possible"] if count["possible"] else None}
    return {"mae": average([error for _, error in errors]),
            "sample_count": len(errors), "missing_rate": diagnostics["missing_rate"], "by_workload": by_workload}


def adoption(baseline: dict, candidate: dict | None, data_reasons: list[str], unit: str = "Cel") -> dict:
    reasons = list(data_reasons)
    threshold = {"Cel": 0.1, "W": 1.0}[unit]
    overall = baseline["mae"]
    improvement = None
    if overall is None:
        reasons.append("missing_baseline_score")
    elif overall < threshold:
        reasons.append("baseline_below_absolute_threshold")
    if not candidate or candidate["mae"] is None:
        reasons.append("missing_candidate_score")
    elif overall is not None and overall >= threshold:
        improvement = (overall - candidate["mae"]) / overall
        if improvement < 0.10 - 1e-12:
            reasons.append("overall_improvement_below_10_percent")
    for name in ("high_load", "transition"):
        before = baseline["by_workload"][name]["mae"]
        after = candidate["by_workload"][name]["mae"] if candidate else None
        if before is None or after is None:
            reasons.append(f"missing_{name}_score")
        elif after > before * 1.05 + 1e-12:
            reasons.append(f"{name}_degradation_above_5_percent")
    return {"adopted": not reasons, "reasons": reasons, "overall_improvement_fraction": improvement}


def label_source_period(labels: list[dict], machine: str, target: str) -> dict | None:
    times = []
    for session in labels:
        if session.get("machine_id") != machine:
            continue
        for event in session.get("transitions", []):
            if event.get("target") != target or event.get("transition_status") != "observed":
                continue
            stamp = datetime.fromisoformat(event["transition_at_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
                raise ValueError("label timestamp must be UTC")
            times.append(stamp)
    return {"first_observed": min(times).isoformat(), "last_observed": max(times).isoformat()} if times else None


def valid_transitions(labels: list[dict], machine: str, target: str, samples: dict[datetime, Sample],
                      start: datetime, end: datetime, signals: list[str]) -> int:
    ids = {}
    times = set()
    boundaries = {"cpu": {("idle", "cpu_load"), ("cpu_load", "cpu_cooldown")},
                  "gpu": {("cpu_cooldown", "gpu_load"), ("gpu_load", "gpu_cooldown")}}
    session_events = set()
    for session in labels:
        if session.get("machine_id") != machine:
            continue
        for event in session.get("transitions", []):
            if event.get("target") != target or event.get("validation") != "valid" or event.get("transition_status") != "observed":
                continue
            edge = (event.get("from_stage"), event.get("to_stage"))
            if edge not in boundaries[target] or not session.get("session_id"):
                raise ValueError("valid transition requires a session and a target-specific stage boundary")
            identifier = event.get("transition_id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("valid transition requires transition_id")
            # Labels can have subsecond timestamps; align forward to telemetry's grid.
            stamp = datetime.fromisoformat(event["transition_at_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
                raise ValueError("transition must be UTC")
            if identifier in ids and ids[identifier] != stamp:
                raise ValueError("transition_id reused at different times")
            ids[identifier] = stamp
            session_edge = (session["session_id"], *edge)
            if session_edge in session_events:
                continue
            session_events.add(session_edge)
            anchor = stamp.replace(microsecond=0) + (timedelta(seconds=1) if stamp.microsecond else timedelta())
            if anchor - timedelta(seconds=INPUT - 1) < start or anchor + timedelta(seconds=HORIZON) >= end:
                continue
            rows = window(samples, anchor, signals)
            future = samples.get(anchor + timedelta(seconds=HORIZON))
            if (rows and future and future.values[0] is not None and
                (rows[-1].boot, rows[-1].config) == (future.boot, future.config) ==
                (session.get("boot_session_id"), session.get("sensor_config_id"))):
                times.add(anchor)
    return len(times)


def train(samples: dict[datetime, Sample], labels: list[dict], machine: str, target: str,
          input_hash: str, label_hash: str | None, *, alpha: float = 1.0,
          train_days: float = 5, validation_days: float = 1, test_days: float = 2) -> tuple[dict, dict]:
    if target not in ("cpu", "gpu"):
        raise ValueError("temperature target must be cpu or gpu")
    if train_days < 5 or validation_days < 1 or test_days < 2:
        raise ValueError("minimum split durations are 5/1/2 days")
    if not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("alpha must be positive and finite")
    # Validate duration inputs even when no target observations were exported.
    chronological_boundaries(datetime(2000, 1, 1, tzinfo=timezone.utc), train_days, validation_days, test_days)
    boundaries = chronological_boundaries(max(samples), train_days, validation_days, test_days) if samples else {}
    signals = select_signals(samples, *boundaries["train"]) if samples else ["temperature"]
    split_examples = {name: [] for name in ("train", "validation", "test")}
    diagnostics = {name: {"possible_count": 0, "valid_count": 0, "missing_rate": None,
                         "temperature_coverage": 0, "candidate_counts_by_workload": {
                             workload: {"possible": 0, "valid": 0} for workload in (*WORKLOADS, "unlabeled")}}
                   for name in split_examples}
    for name, (start, end) in boundaries.items():
        split_examples[name], diagnostics[name] = build_examples(samples, start, end, signals)
    predictors = {name: lambda example, key=name: getattr(example, key) for name in ("persistence", "moving_average")}
    validation_scores = {name: scores(split_examples["validation"], predict, diagnostics["validation"])["mae"] for name, predict in predictors.items()}
    available = [name for name in predictors if validation_scores[name] is not None]
    baseline = min(available, key=lambda name: validation_scores[name]) if available else "persistence"
    reasons = [] if samples else ["no_observations"]
    for name, (start, end) in boundaries.items():
        if not samples or min(samples) > start or max(samples) < end - timedelta(seconds=1):
            reasons.append(f"insufficient_{name}_span")
        if diagnostics[name]["temperature_coverage"] < MIN_COVERAGE or diagnostics[name]["missing_rate"] is None or diagnostics[name]["missing_rate"] > 1 - MIN_COVERAGE + 1e-12:
            reasons.append(f"insufficient_{name}_quality_coverage")
    training = split_examples["train"]
    model = None
    if not reasons and training and len(training) > len(training[0].features):
        try:
            model = fit_ridge(training, alpha)
            predictors["ridge"] = lambda example: predict_model(model, example.features)
        except (ValueError, OverflowError):
            reasons.append("candidate_training_failed")
    elif not reasons:
        reasons.append("insufficient_training_examples")
    transition_count = valid_transitions(labels, machine, target, samples, *boundaries["test"], signals) if samples else 0
    if transition_count < 10:
        reasons.append("insufficient_valid_independent_transitions")
    comparisons = {name: {key: scores(split_examples[name], predict, diagnostics[name]) for key, predict in predictors.items()}
                   for name in split_examples}
    decision = adoption(comparisons["test"][baseline], comparisons["test"].get("ridge"), reasons)
    artifact = {"schema_version": 1, "pipeline_version": VERSION, "machine_id": machine, "target": target,
                "unit": "Cel", "input_seconds": INPUT, "horizon_seconds": HORIZON,
                "signals": signals, "baseline": baseline, "selected_model": "ridge" if decision["adopted"] else baseline,
                "candidate_model": model, "input_sha256": input_hash, "labels_sha256": label_hash}
    report = {**artifact, "generated_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "platform": platform.system(),
                              "os_version": platform.version(), "machine": platform.machine()},
              "config": {"train_days": train_days, "validation_days": validation_days, "test_days": test_days,
                         "stride_seconds": STRIDE, "minimum_coverage": MIN_COVERAGE, "alpha": alpha,
                         "preprocessing_fit_split": "train", "baseline_selection_split": "validation"},
              "boundaries_utc": {name: {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()} for name, (start, end) in boundaries.items()},
              "diagnostics": diagnostics, "metrics": comparisons, "valid_test_transitions": transition_count,
              "data_period_utc": {"first_observed": min(samples).isoformat(), "last_observed": max(samples).isoformat()} if samples else None,
              "label_source_period_utc": label_source_period(labels, machine, target),
              "label_evaluation_period_utc": ({"start_inclusive": boundaries["test"][0].isoformat(),
                                               "end_exclusive": boundaries["test"][1].isoformat()} if samples else None),
              "decision": decision}
    return artifact, report


def infer(artifact: dict, samples: dict[datetime, Sample]) -> dict:
    if artifact.get("pipeline_version") != VERSION or artifact.get("schema_version") != 1:
        raise ValueError("unsupported model artifact")
    if not samples or samples[max(samples)].values[0] is None:
        return {"status": "unavailable", "reason": "latest_temperature_missing"}
    anchor = max(samples)
    selected = artifact["selected_model"]
    # A deployed regression cannot consume incomplete features; use the baseline.
    rows = window(samples, anchor, artifact["signals"])
    served = selected
    if served == "ridge" and rows:
        value = predict_model(artifact["candidate_model"], features(rows, artifact["signals"]))
    else:
        if served == "ridge":
            served = artifact["baseline"]
        temperature_rows = window(samples, anchor, ["temperature"])
        if served == "moving_average" and temperature_rows:
            value = sum(row.values[0] for row in temperature_rows) / INPUT
        else:
            served = "persistence"
            value = samples[anchor].values[0]
    return {"status": "predicted", "served_model": served, "fallback": served != selected,
            "observed_at": anchor.isoformat(), "target_at": (anchor + timedelta(seconds=HORIZON)).isoformat(),
            "temperature_celsius": value}


def comparison_markdown(report: dict) -> str:
    lines = ["# 온도 모델 시간순 평가", "", f"선택 모델: {report['selected_model']}",
             f"채택: {report['decision']['adopted']}", f"사유: {', '.join(report['decision']['reasons']) or '성능·데이터 조건 충족'}",
             f"전체 MAE 개선율: {report['decision']['overall_improvement_fraction']}",
             f"최종 테스트의 유효 독립 전환: {report['valid_test_transitions']}",
             "", "| 구간 | 모델 | 전체 MAE °C | 유휴 | 고부하 | 전환 | 결측률 |", "|---|---|---:|---:|---:|---:|---:|"]
    for split, models in report["metrics"].items():
        for name, metric in models.items():
            values = [metric["mae"], *(metric["by_workload"][workload]["mae"] for workload in WORKLOADS), metric["missing_rate"]]
            lines.append(f"| {split} | {name} | " + " | ".join("미산출" if value is None else f"{value:.6f}" for value in values) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    training = commands.add_parser("train")
    prediction = commands.add_parser("predict")
    for command in (training, prediction):
        command.add_argument("--input", type=Path, required=True)
    training.add_argument("--machine-id", required=True)
    training.add_argument("--target", choices=("cpu", "gpu"), required=True)
    training.add_argument("--labels", type=Path)
    training.add_argument("--output-dir", type=Path, required=True)
    training.add_argument("--alpha", type=float, default=1.0)
    for name, default in (("train", 5), ("validation", 1), ("test", 2)):
        training.add_argument(f"--{name}-days", type=float, default=default)
    prediction.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "predict":
        artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
        samples = load_samples(args.input, artifact["machine_id"], artifact["target"])
        print(json.dumps(infer(artifact, samples), ensure_ascii=False, allow_nan=False))
        return
    samples = load_samples(args.input, args.machine_id, args.target)
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else []
    if isinstance(labels, dict):
        labels = [labels]
    if not isinstance(labels, list) or not all(isinstance(label, dict) for label in labels):
        raise ValueError("labels must be a session object or array of session objects")
    artifact, report = train(samples, labels, args.machine_id, args.target, sha256(args.input),
                             sha256(args.labels) if args.labels else None, alpha=args.alpha,
                             train_days=args.train_days, validation_days=args.validation_days, test_days=args.test_days)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("model.json", artifact), ("evaluation.json", report)):
        (args.output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.output_dir / "comparison.md").write_text(comparison_markdown(report), encoding="utf-8")
    print(json.dumps({"selected_model": artifact["selected_model"], "decision": report["decision"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
