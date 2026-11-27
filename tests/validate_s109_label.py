"""Validate the S1-09 label schema and chronological consistency."""
import argparse
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

SCHEMA = Path(__file__).resolve().parents[1] / "docs/s1-09-label.schema.json"


def utc_key(value):
    # Keep all fractional-second digits when comparing two UTC timestamps.
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", value):
        raise ValueError(f"Invalid UTC timestamp: {value}")
    whole, separator, fraction = value[:-1].partition(".")
    return datetime.fromisoformat(whole), Decimal("0." + fraction) if separator else Decimal(0)


def validate_label(label):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(label)

    previous_end = utc_key(label["run_started_at_utc"])
    run_end = utc_key(label["run_ended_at_utc"])
    if run_end < previous_end:
        raise ValueError("Session end precedes its start")

    previous_status = "completed"
    for index, stage in enumerate(label["stages"]):
        status = stage["stage_status"]
        if index:
            transition = label["transitions"][index - 1]
            expected = "not_observed" if status == "not_started" else "observed"
            if transition["transition_status"] != expected:
                raise ValueError(f"Transition into {stage['stage_id']} must be {expected}")
            if expected == "observed" and utc_key(transition["transition_at_utc"]) != utc_key(stage["start_utc"]):
                raise ValueError(f"Transition into {stage['stage_id']} must match its start")

        if status != "not_started":
            if previous_status != "completed":
                raise ValueError(f"Stage {stage['stage_id']} follows an unfinished stage")
            start, end = utc_key(stage["start_utc"]), utc_key(stage["end_utc"])
            if not previous_end <= start <= end <= run_end:
                raise ValueError(f"Stage {stage['stage_id']} is out of chronological order")
            if status == "completed" and start == end:
                raise ValueError(f"Completed stage {stage['stage_id']} must have positive duration")
            previous_end = end
        previous_status = status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", type=Path)
    args = parser.parse_args()
    try:
        validate_label(json.loads(args.label.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
