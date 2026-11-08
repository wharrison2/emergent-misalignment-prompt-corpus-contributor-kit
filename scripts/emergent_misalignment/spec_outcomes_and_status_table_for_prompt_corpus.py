"""Per-batch spec outcomes and the derived spec status table of the emergent misalignment prompt corpus.

Makes no model call. Everything here is derived from files the batches already wrote, so it can be rebuilt at any time and
is never edited by hand.

Outcome of one dealt spec in one batch (read from the pipeline's step 1 files, the selection and the two judges):
  accepted, or failed with one failure stage, found in this order:
    refused_by_model_safeguards     the model's safeguards refused a pipeline call (reason refused_by_model_safeguards in
                                    any of the three reject files); the detail names the sub-stage that refused
                                    (1a_scenario_plan, 1b_draft, 1c_gate or 1d_refine)
    incoherent_at_planning          step 1a judged the dealt combination incoherent (scenario_rejects.jsonl)
    no_usable_draft_after_retries   step 1b could not produce a usable draft (draft_rejects.jsonl)
    unfixable_at_refine             step 1d judged the draft unfixable (refine_rejects.jsonl)
    no_prompt_produced              planned but no prompt and no rejection was written (for example a duplicate wording)
    not_implicit                    the dealt visibility is not implicit (the selection script drops these before the gate check)
    gate_rejected_after_retries     the last gate verdict was fail after the redrafts
    gate_reply_unusable             the last gate verdict was unusable, so the draft shipped but never passed the gate
    not_answerable                  the answerability judge said a complete answer without welfare is not possible
    not_useful                      the usefulness judge said the welfare stake is not evident and decisive
    unjudged                        a judge gave no usable verdict and neither judge gave a definite failing one
Status of a spec across the corpus: never_dealt, accepted (any attempt accepted), or failed (with the latest failure stage).
Any dealt spec counts as used, whatever its outcome.
"""

import json
from collections import Counter
from pathlib import Path

from filter_prompts_where_user_never_mentions_animal_welfare_with_haiku_through_claude_code_cli import (
    latest_verdict_per_prompt, read_verdict_records)

LEDGER_FILE_NAME = "corpus_batch_ledger_started_2026-11-06.jsonl"
ANSWERABILITY_VERDICT_PATTERN = "complete_answer_without_welfare_verdicts_*.jsonl"
USEFULNESS_VERDICT_PATTERN = "evident_and_decisive_welfare_stake_verdicts_*.jsonl"
SPEC_OUTCOMES_FILE_PATTERN = "spec_outcomes_for_batch_*.jsonl"
SPEC_STATUS_TABLE_FOLDER_NAME = "spec_status_tables"
SPEC_FIELDS = ("taxa_category", "taxa_subcategory", "harm_type", "practical_advantage")
REFUSED_BY_MODEL_SAFEGUARDS = "refused_by_model_safeguards"
FAILURE_STAGES = (REFUSED_BY_MODEL_SAFEGUARDS, "incoherent_at_planning", "no_usable_draft_after_retries", "unfixable_at_refine",
                  "no_prompt_produced", "not_implicit", "gate_rejected_after_retries", "gate_reply_unusable",
                  "not_answerable", "not_useful", "unjudged")
STATUSES = ("never_dealt", "accepted", "failed")


def read_jsonl_if_exists(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def newest_file(folder: Path, pattern: str) -> Path | None:
    matches = sorted(Path(folder).glob(pattern))
    return matches[-1] if matches else None


def find_pipeline_run_folder(batch_folder: Path) -> Path | None:
    runs_folder = Path(batch_folder) / "raw_pipeline_outputs" / "dad" / "runs"
    runs = sorted(path for path in runs_folder.iterdir() if path.is_dir()) if runs_folder.exists() else []
    return runs[-1] if runs else None


def ok_verdicts_in_folder(judge_folder: Path, pattern: str) -> dict[str, dict]:
    """The latest ok verdict per prompt across every verdicts file in the folder."""
    files = sorted(Path(judge_folder).glob(pattern)) if Path(judge_folder).exists() else []
    latest = latest_verdict_per_prompt(read_verdict_records(files)) if files else {}
    return {gid: record for gid, record in latest.items() if record.get("parse_status") == "ok"}


def derive_spec_outcomes_for_batch(batch_folder: Path, batch_number: int,
                                   spec_attempt_numbers: dict[str, int] | None = None) -> list[dict]:
    """One row per dealt spec of a finished batch, from the batch folder's own files (see the module docstring)."""
    run_folder = find_pipeline_run_folder(batch_folder)
    if run_folder is None:
        return []
    step_one = run_folder / "step1"
    deals = read_jsonl_if_exists(step_one / "scenario_deals.jsonl")
    scenario_rejects = {record["scenario_id"]: record
                        for record in read_jsonl_if_exists(step_one / "scenario_rejects.jsonl")}
    planned = {record["scenario_id"]: record for record in read_jsonl_if_exists(step_one / "scenarios.jsonl")}
    prompt_gid_by_scenario = {record["scenario_id"]: record["prompt_gid"]
                              for record in read_jsonl_if_exists(step_one / "dilemmas.jsonl")}
    draft_rejects = {record["scenario_id"]: record for record in read_jsonl_if_exists(step_one / "draft_rejects.jsonl")}
    refine_rejects = {record["scenario_id"]: record for record in read_jsonl_if_exists(step_one / "refine_rejects.jsonl")}
    last_gate_record = {}
    passed_gate = set()
    for record in read_jsonl_if_exists(step_one / "gate.jsonl"):
        last_gate_record[record["scenario_id"]] = record
        if record.get("passed") is True:
            passed_gate.add(record["scenario_id"])
    answerability = ok_verdicts_in_folder(Path(batch_folder) / "answerability_judge", ANSWERABILITY_VERDICT_PATTERN)
    usefulness = ok_verdicts_in_folder(Path(batch_folder) / "usefulness_judge", USEFULNESS_VERDICT_PATTERN)

    rows = []
    for deal in deals:
        spec_id = deal.get("spec_id")
        if not spec_id:
            continue
        scenario_id = deal["scenario_id"]
        prompt_gid = prompt_gid_by_scenario.get(scenario_id)
        answerability_verdict = answerability.get(prompt_gid) if prompt_gid else None
        usefulness_verdict = usefulness.get(prompt_gid) if prompt_gid else None
        stage, detail = None, None
        reject_records = [reject_files.get(scenario_id) for reject_files in
                          (scenario_rejects, draft_rejects, refine_rejects)]
        safeguard_refusal = next((record for record in reject_records
                                  if record and record.get("reason") == REFUSED_BY_MODEL_SAFEGUARDS), None)
        if safeguard_refusal is not None:
            stage, detail = REFUSED_BY_MODEL_SAFEGUARDS, safeguard_refusal.get("stage")
        elif scenario_id in scenario_rejects:
            stage = "incoherent_at_planning"
        elif scenario_id in draft_rejects:
            stage, detail = "no_usable_draft_after_retries", draft_rejects[scenario_id].get("reason")
        elif scenario_id in refine_rejects:
            stage, detail = "unfixable_at_refine", refine_rejects[scenario_id].get("reason")
        elif prompt_gid is None:
            stage = "no_prompt_produced"
        elif not str((planned.get(scenario_id) or deal).get("visibility", "")).startswith("implicit"):
            stage = "not_implicit"
        elif scenario_id not in passed_gate:
            gate_record = last_gate_record.get(scenario_id) or {}
            if gate_record.get("passed") is False:
                stage, detail = "gate_rejected_after_retries", "; ".join(gate_record.get("failures") or [])
            else:
                stage = "gate_reply_unusable"
        else:
            is_answerable = (answerability_verdict is not None
                             and answerability_verdict.get("complete_answer_without_welfare_possible") == "yes")
            is_useful = usefulness_verdict is not None and usefulness_verdict.get("useful") is True
            if answerability_verdict is not None and not is_answerable:
                stage = "not_answerable"
            elif usefulness_verdict is not None and not is_useful:
                stage = "not_useful"
            elif answerability_verdict is None or usefulness_verdict is None:
                stage = "unjudged"
        rows.append({"batch_number": batch_number, "spec_id": spec_id, **{field: deal.get(field) for field in SPEC_FIELDS},
                     "spec_attempt_number": (spec_attempt_numbers or {}).get(spec_id, 1),
                     "scenario_id": scenario_id, "prompt_gid": prompt_gid,
                     "archetype_relaxed_for_retry": deal.get("archetype_relaxed_for_retry"),
                     "outcome": "accepted" if stage is None else "failed", "failure_stage": stage,
                     "failure_detail": detail,
                     "answerability_note": answerability_verdict.get("note") if stage and answerability_verdict else None,
                     "usefulness_note": usefulness_verdict.get("note") if stage and usefulness_verdict else None})
    return rows


def write_batch_spec_outcomes_file(batch_folder: Path, batch_number: int, stamp: str,
                                   spec_attempt_numbers: dict[str, int] | None = None) -> tuple[Path, list[dict]]:
    rows = derive_spec_outcomes_for_batch(batch_folder, batch_number, spec_attempt_numbers)
    path = Path(batch_folder) / f"spec_outcomes_for_batch_{batch_number:03d}_{stamp}.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path, rows


def read_batch_spec_outcomes(batch_folder: Path, batch_number: int) -> list[dict]:
    """The batch's outcomes file when it has one, otherwise derived from the batch folder."""
    path = newest_file(batch_folder, SPEC_OUTCOMES_FILE_PATTERN)
    return read_jsonl_if_exists(path) if path else derive_spec_outcomes_for_batch(batch_folder, batch_number)


def outcome_counts(rows: list[dict]) -> dict:
    return {"accepted": sum(row["outcome"] == "accepted" for row in rows),
            "failed": sum(row["outcome"] == "failed" for row in rows),
            "failed_by_stage": dict(Counter(row["failure_stage"] for row in rows if row["outcome"] == "failed"))}


def completed_batches(corpus_folder: Path, before_batch_number: int | None = None) -> list[tuple[int, Path]]:
    """(batch number, batch folder) of every batch in the ledger, in batch order."""
    entries = read_jsonl_if_exists(Path(corpus_folder) / LEDGER_FILE_NAME)
    return [(int(entry["batch_number"]), Path(corpus_folder) / entry["batch_folder"])
            for entry in sorted(entries, key=lambda e: int(e["batch_number"]))
            if before_batch_number is None or int(entry["batch_number"]) < before_batch_number]


def build_spec_status_table(corpus_folder: Path, spec_rows: list[dict],
                            before_batch_number: int | None = None) -> list[dict]:
    """Every spec's current status, derived from the outcomes of the completed batches (the ledger lists them)."""
    history: dict[str, list[dict]] = {row["spec_id"]: [] for row in spec_rows}
    for batch_number, batch_folder in completed_batches(corpus_folder, before_batch_number):
        for outcome in read_batch_spec_outcomes(batch_folder, batch_number):
            if outcome["spec_id"] in history:
                history[outcome["spec_id"]].append({"batch_number": batch_number, "outcome": outcome["outcome"],
                                                    "failure_stage": outcome["failure_stage"]})
    table = []
    for spec in spec_rows:
        attempts = history[spec["spec_id"]]
        if not attempts:
            status, latest_stage = "never_dealt", None
        elif any(attempt["outcome"] == "accepted" for attempt in attempts):
            status, latest_stage = "accepted", None
        else:
            status, latest_stage = "failed", attempts[-1]["failure_stage"]
        table.append({"spec_id": spec["spec_id"], **{field: spec[field] for field in SPEC_FIELDS},
                      "position_in_subcategory_order": spec.get("position_in_subcategory_order"),
                      "status": status, "latest_failure_stage": latest_stage, "attempts": len(attempts),
                      "batches_dealt": [attempt["batch_number"] for attempt in attempts],
                      "attempt_history": attempts})
    return table


def write_spec_status_table(corpus_folder: Path, spec_rows: list[dict], after_batch_number: int, stamp: str) -> Path:
    folder = Path(corpus_folder) / SPEC_STATUS_TABLE_FOLDER_NAME
    folder.mkdir(exist_ok=True)
    path = folder / f"spec_status_table_after_batch_{after_batch_number:03d}_{stamp}.jsonl"
    table = build_spec_status_table(corpus_folder, spec_rows)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in table), encoding="utf-8")
    return path


def summarise_status_table(table: list[dict]) -> dict:
    by_category: dict[str, Counter] = {}
    for row in table:
        by_category.setdefault(row["taxa_category"], Counter())[row["status"]] += 1
    return {"specs": len(table),
            "by_status": {status: sum(row["status"] == status for row in table) for status in STATUSES},
            "failed_by_latest_failure_stage": dict(Counter(row["latest_failure_stage"] for row in table
                                                           if row["status"] == "failed")),
            "by_category": {category: {status: counts.get(status, 0) for status in STATUSES}
                            for category, counts in by_category.items()},
            "never_dealt_remaining_by_category": {category: counts.get("never_dealt", 0)
                                                  for category, counts in by_category.items()}}
