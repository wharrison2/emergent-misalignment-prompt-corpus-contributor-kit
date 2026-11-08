"""Select the implicit-visibility prompts from a difficult advice pipeline step-1 run.

Joins step1/dilemmas.jsonl (the refined user messages) with step1/scenarios.jsonl
(the dealt cards and scenario description) and step1/gate.jsonl (the gate log),
drops prompts whose visibility is not implicit (the dealer forces trap surface
forms to hidden) and prompts that never passed the gate, and keeps the first
--count by scenario id. Makes no model calls.

Usage:
  python scripts/emergent_misalignment/select_implicit_prompts_from_difficult_advice_step_one_run.py \
      --step-one-folder <run>/step1 --output-folder <run folder> --count 50
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

CARD_FIELDS = ("visibility", "welfare_magnitude", "user_stakes", "user_attitude", "surface_form",
               "conflict", "taxa_category", "taxa_subcategory", "harm_type", "practical_advantage", "spec_id",
               "deliverable", "cultural_setting",
               "archetype", "domain", "user_goal", "leverage", "user_moral_framework",
               "anchor_value_pair", "secondary_value_pair", "claim_pattern",
               "failure_mode_target", "failure_mode_salience", "opening_move", "closing_move",
               "length_class")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gate_summary(gate_records: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for record in gate_records:
        entry = summary.setdefault(record["scenario_id"], {"gate_attempts": 0, "passed_gate": False})
        entry["gate_attempts"] += 1
        if record.get("passed") is True:
            entry["passed_gate"] = True
    return summary


def written_in_english(cultural_setting) -> bool:
    return cultural_setting in (None, "None", "") or "written in English" in str(cultural_setting)


def select_prompts(step_one_folder: Path, count: int) -> tuple[list[dict], dict]:
    dilemmas = read_jsonl(step_one_folder / "dilemmas.jsonl")
    scenarios = {s["scenario_id"]: s for s in read_jsonl(step_one_folder / "scenarios.jsonl")}
    gates = gate_summary(read_jsonl(step_one_folder / "gate.jsonl"))
    selected, dropped = [], {"not_implicit": [], "did_not_pass_gate": [], "beyond_count": []}
    for dilemma in sorted(dilemmas, key=lambda d: d["scenario_id"]):
        scenario = scenarios[dilemma["scenario_id"]]
        gate = gates.get(dilemma["scenario_id"], {"gate_attempts": 0, "passed_gate": False})
        if not str(scenario["visibility"]).startswith("implicit"):
            dropped["not_implicit"].append(dilemma["prompt_gid"])
            continue
        if not gate["passed_gate"]:
            dropped["did_not_pass_gate"].append(dilemma["prompt_gid"])
            continue
        if len(selected) >= count:
            dropped["beyond_count"].append(dilemma["prompt_gid"])
            continue
        record = {"prompt_gid": dilemma["prompt_gid"], "scenario_id": dilemma["scenario_id"],
                  "scenario_gid": dilemma["scenario_gid"], "user_message": dilemma["user_message"],
                  "user_message_word_count": len(dilemma["user_message"].split())}
        record.update({field: scenario.get(field) for field in CARD_FIELDS})
        # Every other dealt axis the scenario carries (welfare_partner, scope, severity, ...), so a newly dealt axis
        # reaches the selected records without editing this script; absent for axes a run did not deal.
        record.update({axis: value for axis, value in (scenario.get("variables") or {}).items()
                       if axis not in record})
        record["written_in_english"] = written_in_english(scenario.get("cultural_setting"))
        record.update(gate)
        record["scenario_description"] = scenario.get("scenario_description")
        selected.append(record)
    return selected, dropped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step-one-folder", type=Path, required=True)
    parser.add_argument("--output-folder", type=Path, required=True)
    parser.add_argument("--count", type=int, default=50)
    args = parser.parse_args()
    selected, dropped = select_prompts(args.step_one_folder, args.count)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    output_path = args.output_folder / f"strictly_implicit_welfare_prompts_{len(selected)}_{stamp}.jsonl"
    output_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in selected), encoding="utf-8")
    summary_path = args.output_folder / f"prompt_selection_summary_{stamp}.json"
    summary_path.write_text(json.dumps({"selected": len(selected), "requested": args.count,
                                        "dropped": dropped}, indent=2), encoding="utf-8")
    print(f"Selected {len(selected)} of {args.count} requested -> {output_path}")
    for reason, ids in dropped.items():
        print(f"  dropped ({reason}): {len(ids)}")
    if len(selected) < args.count:
        print("WARNING: fewer implicit, gate-passed prompts than requested.")


if __name__ == "__main__":
    main()
