"""Regression tests for S1-09 session labels."""
import copy
import unittest
from jsonschema import ValidationError
from validate_s109_label import validate_label

uid = "00000000-0000-4000-8000-000000000001"
stamp = lambda minute: f"2026-11-28T00:{minute:02}:00Z"
stages = []
for i, (key, task, target) in enumerate([
    ("idle", "idle", "none"), ("cpu_load", "cpu_load", "cpu"),
    ("cpu_cooldown", "cooldown", "none"), ("gpu_load", "gpu_load", "gpu"),
    ("gpu_cooldown", "cooldown", "none")]):
    tool = "demo" if target != "none" else None
    stages.append(dict(stage_id=key, task=task, target=target, planned_minutes=10,
        stage_status="completed", start_utc=stamp(i*10), end_utc=stamp((i+1)*10),
        fan=dict(mode="auto", target_percent=None), workload_tool=tool,
        workload_version=tool, workload_profile=tool))
transitions = [dict(transition_id=f"00000000-0000-4000-8000-{i+2:012}", from_stage=stages[i]["stage_id"],
    to_stage=stages[i+1]["stage_id"], target=target, transition_status="observed",
    transition_at_utc=stamp((i+1)*10), validation="pending", exclusion_reason=None)
    for i, target in enumerate(["cpu","cpu","gpu","gpu"])]
label = dict(label_schema_version="s1-09-label-v0.1-draft", session_id=uid,
    machine_alias="PC-A", machine_id=uid, boot_session_id=uid,
    environment=dict(cpu_model="cpu", gpu_model="gpu", os_build="1", agent_version="1", agent_commit="1234567"),
    sensor_config_id=uid, sensors=[dict(sensor_id="cpu",kind="temperature",unit="Cel",support="supported",last_quality="ok")],
    safety_limits={f"{device}_{key}": value for device in ("cpu","gpu") for key, value in
        (("manufacturer_limit_celsius", 90), ("stop_threshold_celsius", 80), ("source","https://example.org"))},
    status="completed", run_started_at_utc=stamp(0), run_ended_at_utc=stamp(50),
    operator_alias="A", stages=stages, transitions=transitions, stop_reason="none", artifacts=[])


class LabelValidationTests(unittest.TestCase):
    def test_completed_label(self):
        validate_label(label)

    def test_invalid_status_or_timestamps(self):
        for key, index, updates in [
            ("stages", 4, dict(stage_status="not_started", start_utc=None, end_utc=None)),
            ("transitions", 0, dict(transition_status="not_observed", transition_at_utc=None, validation="not_applicable")),
            ("transitions", 0, dict(transition_at_utc="2026-11-28T00:10:00.0000001Z")),
            ("stages", 1, dict(end_utc=stamp(5))),
            ("stages", 2, dict(start_utc=stamp(19))),
        ]:
            invalid = copy.deepcopy(label)
            invalid[key][index].update(updates)
            with self.subTest(key=key, index=index, updates=updates):
                with self.assertRaises((ValueError, ValidationError)):
                    validate_label(invalid)

    def test_aborted_label(self):
        aborted = copy.deepcopy(label)
        aborted.update(status="aborted", stop_reason="operator_request", run_ended_at_utc=stamp(5))
        aborted["stages"][0].update(stage_status="aborted", end_utc=stamp(5))
        for stage, transition in zip(aborted["stages"][1:], aborted["transitions"]):
            stage.update(stage_status="not_started", start_utc=None, end_utc=None,
                workload_tool=None, workload_version=None, workload_profile=None)
            transition.update(transition_status="not_observed", transition_at_utc=None, validation="not_applicable")
        validate_label(aborted)

    def test_rejects_aborted_session_with_only_completed_stages(self):
        invalid = copy.deepcopy(label)
        invalid.update(status="aborted", stop_reason="operator_request")
        with self.assertRaises(ValidationError):
            validate_label(invalid)

    def test_rejects_equal_temperature_threshold(self):
        invalid = copy.deepcopy(label)
        invalid["safety_limits"]["cpu_stop_threshold_celsius"] = 90
        with self.assertRaisesRegex(ValueError, "threshold"):
            validate_label(invalid)

    def test_rejects_duplicate_transition_id(self):
        invalid = copy.deepcopy(label)
        invalid["transitions"][1]["transition_id"] = invalid["transitions"][0]["transition_id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_label(invalid)
