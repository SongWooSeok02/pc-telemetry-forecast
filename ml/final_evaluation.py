"""Freeze S3-04 artifacts and score a fresh S4-05 holdout without fitting."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ml import evaluate_baselines as baseline_pipeline
from ml import temperature_model as model_pipeline

VERSION = "s4-05.1"
SECOND = timedelta(seconds=1)
BASELINES = ("persistence", "moving_average")
RULES = {
    "input_seconds": model_pipeline.INPUT,
    "horizon_seconds": model_pipeline.HORIZON,
    "stride_seconds": model_pipeline.STRIDE,
    "minimum_coverage": model_pipeline.MIN_COVERAGE,
    "minimum_independent_transitions": 10,
    "minimum_sessions_per_day": 2,
    "minimum_overall_improvement": 0.10,
    "maximum_subgroup_degradation": 0.05,
    "minimum_baseline_mae_celsius": 0.1,
    "quality_rule": "finite ok samples, ok clock, no imputation, same boot/sensor configuration",
}


class Schedule:
    """One 48h holdout window derived from an explicitly supplied freeze and report deadline.

    No calendar date is built in: the team records the agreed deadlines, passes them to
    `freeze`, and the manifest carries them so `evaluate` scores exactly that window.
    """

    def __init__(self, freeze_deadline: datetime, report_deadline: datetime):
        for value in (freeze_deadline, report_deadline):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("schedule deadlines must include timezone")
            if value.microsecond:
                # Telemetry rows sit on whole seconds; a fractional deadline would shift every
                # boundary off that grid and make complete buffers look incomplete.
                raise ValueError("schedule deadlines must be whole seconds")
        self.freeze_deadline, self.report_deadline = freeze_deadline, report_deadline
        self.data_end = report_deadline - timedelta(minutes=1)
        self.core_end = self.data_end - timedelta(seconds=model_pipeline.HORIZON)
        self.core_start = self.core_end - timedelta(hours=48)
        self.data_start = self.core_start - timedelta(seconds=model_pipeline.INPUT)
        # The freeze itself must still precede data_start (checked in evaluate); the deadline
        # only has to leave room for a holdout, as the original card's 09:00 cap did.
        if freeze_deadline >= self.data_end:
            raise ValueError("freeze deadline must be before the holdout data end "
                             f"{self.data_end.isoformat()}")

    def policy(self) -> dict:
        return RULES | {
            "freeze_deadline": self.freeze_deadline.isoformat(),
            "core_start": self.core_start.isoformat(),
            "core_end_exclusive": self.core_end.isoformat(),
            "data_start": self.data_start.isoformat(),
            "data_end_exclusive": self.data_end.isoformat(),
            "report_deadline": self.report_deadline.isoformat(),
        }

    @classmethod
    def from_policy(cls, policy: dict) -> "Schedule":
        """Rebuild the frozen schedule and reject a manifest whose policy was edited."""
        try:
            schedule = cls(date(policy["freeze_deadline"]), date(policy["report_deadline"]))
        except (KeyError, TypeError) as error:
            raise ValueError("freeze mismatch: manifest has no schedule; a new freeze is required") from error
        if policy != schedule.policy():
            raise ValueError("freeze mismatch: model or evaluation changed; a new holdout is required")
        return schedule


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict:
    return {Path(module.__file__).name: digest(Path(module.__file__).read_bytes())
            for module in (model_pipeline, baseline_pipeline)} | {
                "final_evaluation.py": digest(Path(__file__).read_bytes())}


def date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def encode(payload: dict) -> bytes:
    # Serialize fully before touching disk so invalid payloads never leave a truncated file.
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _stage(path: Path, data: bytes) -> Path:
    """Write data to a unique, durable temporary file beside path."""
    staged = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.partial")
    try:
        with staged.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def describe_occupied(path: Path) -> str | None:
    """Describe what occupies path, including symlinks whose target is missing.

    Path.exists() follows symlinks and reports a dangling link as free, so use lexists().
    """
    if path.is_symlink():
        return "symlink" if path.exists() else "dangling symlink"
    return "file" if os.path.lexists(path) else None


def write_new_files(outputs: list[tuple[Path, bytes]]) -> None:
    """Publish all outputs or none of them, never overwriting an existing path.

    Each file is staged completely and then hard-linked into place, which fails if the
    destination exists. If any step fails, files already published by this call are
    removed so a retry with the same paths is not blocked by a half-written result.
    """
    paths = [path.resolve() for path, _ in outputs]
    if len(set(paths)) != len(paths):
        raise ValueError("output paths must be distinct")
    for path, _ in outputs:
        occupied = describe_occupied(path)
        if occupied:
            raise FileExistsError(f"output already exists ({occupied}): {path}")
    staged: list[Path] = []
    published: list[Path] = []
    try:
        for path, data in outputs:
            staged.append(_stage(path, data))
        for (path, _), source in zip(outputs, staged):
            os.link(source, path)
            published.append(path)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise
    finally:
        for source in staged:
            source.unlink(missing_ok=True)


def write_new(path: Path, payload: dict) -> None:
    write_new_files([(path, encode(payload))])


def model_contract(artifact: dict) -> None:
    signals = artifact["signals"]
    if (artifact["schema_version"] != 1 or artifact["pipeline_version"] != model_pipeline.VERSION
            or artifact["unit"] != "Cel" or artifact["target"] not in ("cpu", "gpu")
            or not isinstance(artifact["machine_id"], str) or not artifact["machine_id"]
            or artifact["baseline"] not in BASELINES
            or artifact["input_seconds"] != model_pipeline.INPUT
            or artifact["horizon_seconds"] != model_pipeline.HORIZON
            or not isinstance(signals, list) or not signals or signals[0] != "temperature"
            or signals != [name for name in model_pipeline.SIGNALS if name in signals]):
        raise ValueError("unsupported S3-04 model artifact")
    candidate = artifact["candidate_model"]
    dimensions = 5 + 3 * (len(signals) - 1)
    if candidate is not None:
        for key in ("coefficients", "means", "scales"):
            values = candidate[key]
            if len(values) != dimensions or not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                raise ValueError(f"invalid frozen {key}")
        if min(candidate["scales"]) <= 0 or not math.isfinite(candidate["intercept"]):
            raise ValueError("invalid frozen preprocessing")


def make_freeze(artifact_bytes: bytes, development_bytes: bytes, reviewed_through: datetime,
                now: datetime, schedule: Schedule) -> dict:
    artifact = json.loads(artifact_bytes)
    development = json.loads(development_bytes)
    model_contract(artifact)
    if any(development.get(key) != value for key, value in artifact.items()):
        raise ValueError("S3-04 development report and model do not match")
    periods = development["boundaries_utc"]
    if set(periods) != {"train", "validation", "test"}:
        raise ValueError("development report must include all three splits")
    if artifact["candidate_model"] is not None:
        validation = development["metrics"]["validation"]
        reference = {name: validation[name]["mae"] for name in BASELINES}
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in reference.values()) or min(BASELINES, key=reference.__getitem__) != artifact["baseline"]:
            raise ValueError("frozen baseline does not match development validation scores")
    previous_end = max(date(period["end_exclusive"]) for period in periods.values())
    observed = development.get("data_period_utc")
    if not observed:
        raise ValueError("development data provenance is missing")
    latest_observed = date(observed["last_observed"])
    labels = development.get("label_source_period_utc")
    last_label = date(labels["last_observed"]) if labels else latest_observed
    report_created = date(development["generated_at"])
    if (max(previous_end, latest_observed + SECOND, last_label) > reviewed_through
            or not reviewed_through < now or report_created > now):
        raise ValueError("development or tuning provenance extends beyond freeze")
    return {
        "version": VERSION, "frozen_at": now.isoformat(), "reviewed_through": reviewed_through.isoformat(),
        "machine_id": artifact["machine_id"], "target": artifact["target"],
        "baseline": artifact["baseline"], "features": artifact["signals"],
        "artifact_sha256": digest(artifact_bytes), "development_report_sha256": digest(development_bytes),
        "development_input_sha256": artifact.get("input_sha256"),
        "development_labels_sha256": artifact.get("labels_sha256"),
        "source_sha256": source_hashes(), "policy": schedule.policy(),
    }


def session_counts(labels: list[dict], machine: str, start: datetime, end: datetime) -> list[int]:
    sessions: dict[str, tuple[datetime, datetime]] = {}
    for label in labels:
        if label.get("machine_id") != machine or label.get("status") != "completed":
            continue
        session = label["session_id"]
        began, finished = date(label["run_started_at_utc"]), date(label["run_ended_at_utc"])
        if not start <= began < finished <= end:
            continue
        if session in sessions and sessions[session] != (began, finished):
            raise ValueError("session id reused with different times")
        sessions[session] = began, finished
    ordered = sorted(sessions.values())
    if any(previous[1] > following[0] for previous, following in zip(ordered, ordered[1:])):
        raise ValueError("overlapping scenario sessions")
    boundary = start + timedelta(hours=24)
    return [sum(began < boundary and finished <= boundary for began, finished in ordered),
            sum(began >= boundary for began, _ in ordered)]


def boundary_complete(samples: dict, start: datetime, end: datetime) -> bool:
    return all((row := samples.get(start + i * SECOND)) is not None and row.values[0] is not None
               for i in range(int((end - start).total_seconds())))


def evaluate(freeze: dict, artifact_bytes: bytes, samples: dict, labels: list[dict],
             input_hash: str | None, label_hash: str | None, now: datetime,
             freeze_evidence: str | None) -> dict:
    artifact = json.loads(artifact_bytes)
    model_contract(artifact)
    if freeze["version"] != VERSION:
        raise ValueError("freeze mismatch: model or evaluation changed; a new holdout is required")
    schedule = Schedule.from_policy(freeze["policy"])
    FREEZE_DEADLINE, REPORT_DEADLINE = schedule.freeze_deadline, schedule.report_deadline
    DATA_START, DATA_END = schedule.data_start, schedule.data_end
    CORE_START, CORE_END = schedule.core_start, schedule.core_end
    if (freeze["source_sha256"] != source_hashes()
            or freeze["artifact_sha256"] != digest(artifact_bytes)
            or any(freeze[key] != artifact[key] for key in ("machine_id", "target", "baseline"))
            or freeze["features"] != artifact["signals"]):
        raise ValueError("freeze mismatch: model or evaluation changed; a new holdout is required")
    frozen = date(freeze["frozen_at"])
    reviewed = date(freeze["reviewed_through"])
    reasons = []
    if not freeze_evidence or not freeze_evidence.strip():
        reasons.append("missing_independent_freeze_evidence")
    if frozen > FREEZE_DEADLINE or frozen > DATA_START or reviewed >= frozen:
        reasons.append("freeze_schedule_missed")
    if now < DATA_END:
        reasons.append("holdout_not_finished")
        samples, labels = {}, []  # Never expose interim holdout metrics.
    if now > REPORT_DEADLINE:
        reasons.append("report_deadline_missed")
    if not input_hash:
        reasons.append("missing_holdout_data")
    if not label_hash:
        reasons.append("missing_holdout_labels")
    if (input_hash and input_hash == freeze["development_input_sha256"]
            or label_hash and label_hash == freeze["development_labels_sha256"]):
        reasons.append("development_inputs_reused")
    if any(not DATA_START <= stamp < DATA_END or stamp < frozen for stamp in samples):
        reasons.append("samples_outside_fresh_holdout")
    samples = {stamp: row for stamp, row in samples.items()
               if DATA_START <= stamp < DATA_END and frozen <= stamp}
    before = boundary_complete(samples, DATA_START, CORE_START)
    after = boundary_complete(samples, CORE_END, DATA_END)
    if not before or not after:
        reasons.append("missing_boundary_buffer")
    if CORE_START not in samples or CORE_END - SECOND not in samples:
        reasons.append("incomplete_48h_span")

    machine, target = artifact["machine_id"], artifact["target"]
    daily = session_counts(labels, machine, CORE_START, CORE_END)
    if min(daily) < RULES["minimum_sessions_per_day"]:
        reasons.append("insufficient_daily_sessions")
    signals = artifact["signals"]
    target_labels = []
    for label in labels:
        if label.get("machine_id") != machine:
            continue
        began, ended = date(label["run_started_at_utc"]), date(label["run_ended_at_utc"])
        if CORE_START <= began < ended <= CORE_END:
            for event in label["transitions"]:
                if event.get("transition_status") == "observed":
                    if not began <= date(event["transition_at_utc"]) < ended:
                        raise ValueError("observed transition outside its scenario")
            target_labels.append(label)
    transitions = model_pipeline.valid_transitions(target_labels, machine, target, samples, CORE_START, CORE_END, signals)
    if transitions < RULES["minimum_independent_transitions"]:
        reasons.append("insufficient_valid_independent_transitions")

    examples, diagnostics = model_pipeline.build_examples(samples, CORE_START, CORE_END, signals)
    if (diagnostics["temperature_coverage"] < model_pipeline.MIN_COVERAGE
            or diagnostics["missing_rate"] is None
            or diagnostics["missing_rate"] > 1 - model_pipeline.MIN_COVERAGE + 1e-12):
        reasons.append("insufficient_quality_coverage")
    metrics = {name: model_pipeline.scores(examples, lambda example, key=name: getattr(example, key), diagnostics)
               for name in BASELINES}
    candidate = artifact["candidate_model"]
    if candidate is not None:
        try:
            metrics["ridge"] = model_pipeline.scores(
                examples, lambda example: model_pipeline.predict_model(candidate, example.features), diagnostics)
        except (ValueError, OverflowError):
            reasons.append("candidate_prediction_failed")
    decision = model_pipeline.adoption(metrics[artifact["baseline"]], metrics.get("ridge"), reasons)
    return {
        "version": VERSION, "model_version": artifact["pipeline_version"], "generated_at": now.isoformat(),
        "environment": {"os": platform.platform(), "python": platform.python_version()},
        "machine_id": machine, "target": target, "freeze": freeze, "freeze_evidence": freeze_evidence,
        "holdout_input_sha256": input_hash, "holdout_labels_sha256": label_hash,
        "observed_period": ([min(samples).isoformat(), max(samples).isoformat()] if samples else None),
        "label_period_utc": model_pipeline.label_source_period(target_labels, machine, target),
        "boundary_coverage": {"input_5min": before, "horizon_60sec": after},
        "completed_sessions_by_day": daily, "planned_transitions_minimum": 16, "valid_target_transitions": transitions,
        "diagnostics": diagnostics, "metrics": metrics, "decision": decision,
        "release_model": "ridge" if decision["adopted"] else artifact["baseline"],
    }


def publish_evaluation(report_path: Path, report: dict, release_path: Path, release: dict) -> None:
    """Write the evaluation report and release artifact as one all-or-nothing pair.

    The release links to the exact report bytes and is published last, so a release file
    only appears once its report is in place. A leftover report without a release (only
    possible if the process is killed between the two links) is an incomplete run.
    """
    if report_path.resolve() == release_path.resolve():
        raise ValueError("report and release need distinct, unused output paths")
    report_used, release_used = describe_occupied(report_path), describe_occupied(release_path)
    if report_used and not release_used:
        raise ValueError(f"incomplete previous run: {report_path} ({report_used}) exists without "
                         f"{release_path}; inspect and remove it, then rerun")
    if release_used and not report_used:
        raise ValueError(f"orphaned release: {release_path} ({release_used}) exists without its report "
                         f"{report_path}; it is not a valid S4-05 release, do not deploy it, inspect and "
                         "remove it, then rerun")
    if report_used and release_used:
        raise ValueError(f"report and release need distinct, unused output paths: {report_path} "
                         f"({report_used}) and {release_path} ({release_used}) already exist")
    report_bytes = encode(report)
    release = {**release, "s4_05": {**release["s4_05"], "evaluation_report_sha256": digest(report_bytes)}}
    write_new_files([(report_path, report_bytes), (release_path, encode(release))])


def release_matches_report(release_bytes: bytes, report_bytes: bytes) -> bool:
    """True only for a release produced together with exactly this evaluation report."""
    release = json.loads(release_bytes)
    report = json.loads(report_bytes)
    s4_05 = release.get("s4_05") or {}
    return (s4_05.get("evaluation_report_sha256") == digest(report_bytes)
            and s4_05.get("freeze_manifest_sha256") == report.get("freeze_manifest_sha256")
            and s4_05.get("decision") == report.get("decision")
            and release.get("selected_model") == report.get("release_model"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    freeze = modes.add_parser("freeze")
    freeze.add_argument("--development-report", type=Path, required=True)
    freeze.add_argument("--reviewed-through", type=date, required=True)
    freeze.add_argument("--freeze-deadline", type=date, required=True,
                        help="agreed latest freeze time, with timezone (format YYYY-MM-DDTHH:MM+09:00)")
    freeze.add_argument("--report-deadline", type=date, required=True,
                        help="agreed report deadline, with timezone; the 48h holdout ends 2 minutes before it")
    score = modes.add_parser("evaluate")
    score.add_argument("--freeze", type=Path, required=True)
    score.add_argument("--input", type=Path)
    score.add_argument("--labels", type=Path)
    score.add_argument("--expected-freeze-sha256", help="manifest SHA-256 from the independently checked pre-holdout record")
    score.add_argument("--release-output", type=Path, required=True)
    score.add_argument("--freeze-evidence", help="immutable pre-holdout record of the manifest SHA-256, independently checked")
    fallback = modes.add_parser("baseline-release", help="export persistence when no verified model exists")
    fallback.add_argument("--machine-id", required=True)
    fallback.add_argument("--target", choices=("cpu", "gpu"), required=True)
    fallback.add_argument("--output", type=Path, required=True)
    for mode in (freeze, score):
        mode.add_argument("--model", type=Path, required=True)
        mode.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "baseline-release":
        write_new(args.output, {
            "schema_version": 1, "pipeline_version": model_pipeline.VERSION,
            "machine_id": args.machine_id, "target": args.target, "unit": "Cel",
            "input_seconds": model_pipeline.INPUT, "horizon_seconds": model_pipeline.HORIZON,
            "signals": ["temperature"], "baseline": "persistence", "selected_model": "persistence",
            "candidate_model": None, "s4_05_reason": "no_verified_frozen_model",
        })
        return
    artifact = args.model.read_bytes()
    now = datetime.now(timezone.utc)
    if args.mode == "freeze":
        manifest = make_freeze(artifact, args.development_report.read_bytes(), args.reviewed_through, now,
                               Schedule(args.freeze_deadline, args.report_deadline))
        write_new(args.output, manifest)
        print(f"Freeze manifest SHA-256 {model_pipeline.sha256(args.output)}")
        return
    manifest_bytes = args.freeze.read_bytes()
    manifest_hash = digest(manifest_bytes)
    if args.expected_freeze_sha256 and args.expected_freeze_sha256.lower() != manifest_hash:
        raise ValueError("freeze manifest differs from the independently recorded hash")
    manifest = json.loads(manifest_bytes)
    samples = model_pipeline.load_samples(args.input, manifest["machine_id"], manifest["target"]) if args.input else {}
    labels = json.loads(args.labels.read_bytes()) if args.labels else []
    if isinstance(labels, dict):
        labels = [labels]
    if not isinstance(labels, list) or not all(isinstance(label, dict) for label in labels):
        raise ValueError("labels must contain S1-09 session objects")
    report = evaluate(manifest, artifact, samples, labels,
                      model_pipeline.sha256(args.input) if args.input else None,
                      model_pipeline.sha256(args.labels) if args.labels else None, datetime.now(timezone.utc),
                      args.freeze_evidence if args.expected_freeze_sha256 else None)
    # The deadline governs the decision timestamp at this last check before publication;
    # writing the report/release pair to disk happens afterwards and is not time-bounded.
    if datetime.now(timezone.utc) > Schedule.from_policy(manifest["policy"]).report_deadline and "report_deadline_missed" not in report["decision"]["reasons"]:
        report["decision"]["reasons"].append("report_deadline_missed")
        report["decision"]["adopted"] = False
        report["release_model"] = manifest["baseline"]
    report["freeze_manifest_sha256"] = manifest_hash
    release = json.loads(artifact)
    release["selected_model"] = report["release_model"]
    release["s4_05"] = {
        "freeze_manifest_sha256": manifest_hash,
        "holdout_input_sha256": report["holdout_input_sha256"],
        "holdout_labels_sha256": report["holdout_labels_sha256"],
        "decision": report["decision"],
    }
    if not report["decision"]["adopted"]:
        release["candidate_model"] = None
    publish_evaluation(args.output, report, args.release_output, release)
    print(json.dumps({"release_model": report["release_model"], "decision": report["decision"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
