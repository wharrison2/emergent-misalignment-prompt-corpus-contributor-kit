"""Rewrite one control rewrite share of accepted treatment prompts into matched control prompts, as one batch.

A treatment prompt is a user message with an implicit animal welfare stake. A matched control keeps the person, the setting and
the animals and removes the stake. Each prompt of the share gets ONE rewrite attempt (never a second rewrite after a judge
failure), then the rewrite is judged. The corpus folder layout, completion markers, ledger line, usage limit handling and resume
are those of add_batch_to_emergent_misalignment_prompt_corpus.py, so the contributor kit's run_my_shares.py (status, resume, lock,
zip) works on it unchanged.

Corpus folder (--corpus-folder): holds control_rewrite_corpus_settings_share_file_and_share_number.json (share number, the
share file's path inside the folder and its sha256; written by run_my_shares.py --control-rewrite), the share file itself, and
grows a batches/ folder and the ledger corpus_batch_ledger_started_2026-11-06.jsonl.

Steps (each leaves a completion marker in the batch folder, so --resume-batch-folder skips finished ones; inside a step every
finished prompt is saved as it finishes, so a stopped step resumes where it stopped):
  snapshot the share's prompts and the four prompt files (with sha256) -> rewrite (claude-sonnet-5-5, base template version 9,
  strategy replace_the_practice_and_keep_the_animals_version_9) -> no-stake judge (claude-sonnet-5-5, template version 4; only
  rewrites the rewriter did not decline) -> animal role label (claude-haiku-5-5; every rewrite with a judge verdict) ->
  all records file, accepted controls file, summary, ledger line (last).
Accepted = the no-stake judge's passes_allowing_background_harm is true. The animal role label (subject, supporting or scenery)
is recorded on every judged rewrite and never filters. A rewrite the model declined (can_be_rewritten no) is recorded and not
judged. A call whose reply cannot be parsed is retried (two attempts per call); a prompt still without a usable reply after
three runs is recorded as failed and counted in the summary, so one bad prompt never blocks a share. When any prompt has no
record at all from a step, the batch stops before writing the accepted file, summary and ledger line and clears that step's
completion marker, so the rerun redoes it. If the subscription usage limit is reached the batch stops cleanly with exit code 3;
run the same command again to resume.

Every call goes through the Claude Code command line adapter (subscription only: the paid API variables are blanked there).
This script makes no other model call. Tokens and the cost the command line reports are logged per call in
control_rewrite_usage_log_<stamp>.jsonl and totalled in the summary. Use --dry-run to print the plan and one rendered prompt
and create nothing.

Output files in the batch folder (every name ends with its date and time):
  control_rewrite_share_prompts_used_for_this_batch_<stamp>/ the share file and the prompt files used, with
      sha256_of_files_used_for_this_batch_<stamp>.json
  control_rewrites_<stamp>.jsonl, no_stake_verdicts_<stamp>.jsonl, animal_role_labels_<stamp>.jsonl, control_rewrite_usage_log_<stamp>.jsonl
  control_rewrite_all_records_batch_<NNN>_<stamp>.jsonl      every prompt with its rewrite, decline, verdict, label and pass flag
  accepted_control_prompts_batch_<NNN>_<stamp>.jsonl         each control paired to its source by corpus_prompt_id and unique_prompt_id
  control_rewrite_batch_summary_<stamp>.json
"""

import argparse
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))
from add_batch_to_emergent_misalignment_prompt_corpus import (  # noqa: E402
    PROGRESS_REPORT_INTERVAL_SECONDS, TIMESTAMP_FORMAT, USAGE_LIMIT_EXIT_CODE, BatchFailure, BatchStoppedForUsageLimit,
    append_ledger_line, batch_numbers_in_ledger, ledger_batch_folder_text, marker_exists, read_jsonl, read_ledger,
    sha256_of_file, write_marker)
from claude_code_cli_structured_reply_without_paid_api_fallback import (  # noqa: E402
    ClaudeCodeCallFailed, ClaudeCodeUsageLimitReached)
from control_prompt_rewrite_and_judge_reply_helpers import (  # noqa: E402
    ANIMAL_ROLE_JSON_SCHEMA, ANIMAL_ROLE_TEMPLATE_NAME, ANIMAL_ROLE_VALUES, BASE_TEMPLATE_FILE_NAME_PER_STRATEGY,
    JUDGE_ENUM_VALUES_PER_FIELD, JUDGE_JSON_SCHEMA, REWRITE_JSON_SCHEMA, STRATEGY_INSTRUCTIONS_NAME_PATTERN,
    check_animal_role_reply, check_judge_reply, check_rewrite_reply, passes_allowing_background_harm, passes_strict,
    render_animal_role_prompt, render_judge_prompt, render_rewrite_prompt)
from spec_outcomes_and_status_table_for_prompt_corpus import LEDGER_FILE_NAME, newest_file  # noqa: E402
from windows_extended_length_paths import (  # noqa: E402
    extended_length_path, remove_extended_length_prefixes_from_text)

REWRITE_MODEL = "claude-sonnet-5-5"
NO_STAKE_JUDGE_MODEL = "claude-sonnet-5-5"
ANIMAL_ROLE_MODEL = "claude-haiku-5-5"
REWRITE_STRATEGY = "replace_the_practice_and_keep_the_animals_version_9"
NO_STAKE_JUDGE_TEMPLATE_VERSION_4_NAME = "judge_whether_prompt_carries_no_animal_welfare_stake_prompt_template_version_4.txt"
PROMPT_FILE_NAMES_USED = [BASE_TEMPLATE_FILE_NAME_PER_STRATEGY[REWRITE_STRATEGY],
                          STRATEGY_INSTRUCTIONS_NAME_PATTERN.format(strategy=REWRITE_STRATEGY),
                          NO_STAKE_JUDGE_TEMPLATE_VERSION_4_NAME, ANIMAL_ROLE_TEMPLATE_NAME]
PROMPT_FOLDER = SCRIPT_DIRECTORY / "prompts"
CONTROL_SETTINGS_FILE_NAME = "control_rewrite_corpus_settings_share_file_and_share_number.json"
SHARE_FILE_FIELDS = ["corpus_prompt_id", "unique_prompt_id", "user_message", "spec_id", "harm_type", "taxa_category",
                     "taxa_subcategory"]
MAXIMUM_ATTEMPTS_PER_CALL = 2
MAXIMUM_RECORDS_OF_FAILURE_BEFORE_GIVING_UP = 3  # a prompt that failed in three runs is recorded as failed
PARALLEL_CALLS = 8
BATCH_FOLDER_PATTERN = re.compile(r"^batch_(\d{3,})_control_rewrite_share_(\d{3})_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")
SNAPSHOT_FOLDER_PREFIX = "control_rewrite_share_prompts_used_for_this_batch"
SHARE_FILE_SNAPSHOT_FOLDER_NAME = "share_file_and_prompt_files"

REWRITES_PREFIX = "control_rewrites"
VERDICTS_PREFIX = "no_stake_verdicts"
LABELS_PREFIX = "animal_role_labels"
USAGE_PREFIX = "control_rewrite_usage_log"
STEP_REWRITE, STEP_JUDGE, STEP_LABEL = "rewrite", "no_stake_judge", "animal_role_label"
STEP_SNAPSHOT = "snapshot_share_and_prompt_files"
STEP_PREFIXES = {STEP_REWRITE: REWRITES_PREFIX, STEP_JUDGE: VERDICTS_PREFIX, STEP_LABEL: LABELS_PREFIX}

OUTCOME_ACCEPTED = "accepted"
OUTCOME_DECLINED = "declined_by_rewriter"
OUTCOME_REWRITE_FAILED = "rewrite_failed_after_every_attempt"
OUTCOME_JUDGE_FAILED = "judge_failed_after_every_attempt"
OUTCOME_REJECTED = "rejected_by_no_stake_judge"


# ---------------------------------------------------------------- the corpus folder and the share

def read_control_settings(corpus_folder: Path) -> dict:
    path = Path(corpus_folder) / CONTROL_SETTINGS_FILE_NAME
    if not path.exists():
        raise BatchFailure(f"{path} does not exist: this is not a control rewrite share folder (run_my_shares.py --control-rewrite "
                           f"writes it)")
    return json.loads(path.read_text(encoding="utf-8"))


def read_verified_share_records(corpus_folder: Path, settings: dict) -> list[dict]:
    share_path = Path(corpus_folder) / settings["share_file"]
    if not share_path.exists():
        raise BatchFailure(f"the share file {settings['share_file']} is missing from {corpus_folder}")
    if sha256_of_file(share_path) != settings["share_file_sha256"]:
        raise BatchFailure(f"the share file {settings['share_file']} does not match its checksum in the corpus settings; "
                           f"do not edit share files")
    records = read_jsonl(share_path)
    identifiers = [record["unique_prompt_id"] for record in records]
    if len(set(identifiers)) != len(identifiers):
        raise BatchFailure("the share file repeats a unique_prompt_id")
    for record in records:
        missing = [field for field in SHARE_FILE_FIELDS if field not in record]
        if missing:
            raise BatchFailure(f"share record {record.get('unique_prompt_id')} lacks fields {missing}")
    return records


def batch_folders_present(corpus_folder: Path) -> dict[int, Path]:
    batches_folder = Path(corpus_folder) / "batches"
    found = {}
    if batches_folder.exists():
        for folder in sorted(batches_folder.iterdir()):
            match = BATCH_FOLDER_PATTERN.match(folder.name)
            if folder.is_dir() and match:
                found[int(match.group(1))] = folder
    return found


def incomplete_batch_folders(corpus_folder: Path) -> list[Path]:
    completed = batch_numbers_in_ledger(Path(corpus_folder) / LEDGER_FILE_NAME)
    return [folder for number, folder in batch_folders_present(corpus_folder).items() if number not in completed]


def next_batch_number(corpus_folder: Path) -> int:
    known = batch_numbers_in_ledger(Path(corpus_folder) / LEDGER_FILE_NAME) | set(batch_folders_present(corpus_folder))
    return max(known, default=0) + 1


def batch_folder_name(batch_number: int, share_number: int, stamp: str) -> str:
    return f"batch_{batch_number:03d}_control_rewrite_share_{share_number:03d}_{stamp}"


# ---------------------------------------------------------------- prompt files

def prompt_file_hashes(prompt_folder: Path = PROMPT_FOLDER) -> dict[str, str]:
    return {name: sha256_of_file(Path(prompt_folder) / name) for name in PROMPT_FILE_NAMES_USED}


def snapshot_share_and_prompt_files(batch_folder: Path, corpus_folder: Path, settings: dict, stamp: str,
                                    prompt_folder: Path = PROMPT_FOLDER) -> None:
    destination = Path(batch_folder) / f"{SNAPSHOT_FOLDER_PREFIX}_{stamp}"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(corpus_folder) / settings["share_file"], destination / Path(settings["share_file"]).name)
    for name in PROMPT_FILE_NAMES_USED:
        shutil.copy2(Path(prompt_folder) / name, destination / name)
    hashes = {"share_file": {Path(settings["share_file"]).name: settings["share_file_sha256"]},
              "prompt_files": prompt_file_hashes(prompt_folder)}
    (destination / f"sha256_of_files_used_for_this_batch_{stamp}.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")


def snapshot_hashes(batch_folder: Path) -> dict:
    snapshot_folder = newest_file(batch_folder, f"{SNAPSHOT_FOLDER_PREFIX}_*")
    return json.loads(newest_file(snapshot_folder, "sha256_of_files_used_for_this_batch_*.json").read_text(encoding="utf-8"))


def refuse_prompt_files_changed_since_the_batch_started(batch_folder: Path, prompt_folder: Path = PROMPT_FOLDER) -> None:
    recorded = snapshot_hashes(batch_folder)["prompt_files"]
    current = prompt_file_hashes(prompt_folder)
    changed = sorted(name for name in current if recorded.get(name) != current[name])
    if changed:
        raise BatchFailure(f"the prompt files {changed} differ from the ones this batch started with, so resuming would mix two "
                           f"prompt versions. Pull the kit version the batch started with, or ask the person who sent you the kit.")


# ---------------------------------------------------------------- records saved as each call finishes

def find_existing_file(batch_folder: Path, prefix: str) -> Path | None:
    return newest_file(batch_folder, f"{prefix}_20*.jsonl")


def final_record_per_prompt(records: list[dict]) -> dict[str, dict]:
    """The settled result of each prompt: its latest ok record, else (once it has failed in
    MAXIMUM_RECORDS_OF_FAILURE_BEFORE_GIVING_UP runs) its latest failed record. A prompt with fewer failures and no ok record is
    not settled and is attempted again."""
    ok_records, failed_records = {}, {}
    failure_counts: dict[str, int] = {}
    for record in records:
        identifier = record["unique_prompt_id"]
        if record["parse_status"] == "ok":
            ok_records[identifier] = record
        else:
            failure_counts[identifier] = failure_counts.get(identifier, 0) + 1
            failed_records[identifier] = record
    settled = dict(ok_records)
    for identifier, count in failure_counts.items():
        if identifier not in settled and count >= MAXIMUM_RECORDS_OF_FAILURE_BEFORE_GIVING_UP:
            settled[identifier] = failed_records[identifier]
    return settled


def call_until_parsed(step: str, item_id: str, prompt: str, json_schema: dict, check_reply, model: str, call_model, now):
    """Up to MAXIMUM_ATTEMPTS_PER_CALL attempts. Returns (parsed or None, raw replies, usage records, last problem). A failed
    call (not a usage limit) counts as an attempt; the usage limit is never caught here."""
    raw_replies, usage_records, parsed, problem = [], [], None, None
    for attempt_number in range(1, MAXIMUM_ATTEMPTS_PER_CALL + 1):
        usage = {"step": step, "item_id": item_id, "attempt": attempt_number, "model": model, "input_tokens": 0,
                 "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                 "cli_reported_cost_usd": None}
        try:
            reply = call_model(model, prompt, json_schema)
        except ClaudeCodeCallFailed as error:
            problem = f"call failed: {error}"
            usage.update(problem=problem, logged_at=now().isoformat(timespec="seconds"))
            usage_records.append(usage)
            continue
        raw_replies.append(reply.reply_text)
        parsed, problem = check_reply(reply.reply_text)
        usage.update(input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                     cache_read_input_tokens=reply.cache_read_input_tokens,
                     cache_creation_input_tokens=reply.cache_creation_input_tokens,
                     cli_reported_cost_usd=reply.reported_cost_usd, problem=problem,
                     logged_at=now().isoformat(timespec="seconds"))
        usage_records.append(usage)
        if parsed is not None:
            break
    return parsed, raw_replies, usage_records, problem


def add_usage_totals(record: dict, usage_records: list[dict]) -> None:
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        record[field] = sum(usage[field] for usage in usage_records)
    record["cli_reported_cost_usd"] = sum(usage["cli_reported_cost_usd"] or 0.0 for usage in usage_records)


def parse_status_of(parsed, raw_replies) -> str:
    return "ok" if parsed else ("unparseable" if raw_replies else "call_failed")


def rewrite_one_prompt(share_record: dict, call_model, now):
    prompt = render_rewrite_prompt(share_record, REWRITE_STRATEGY)
    parsed, raw_replies, usage_records, problem = call_until_parsed(
        STEP_REWRITE, share_record["unique_prompt_id"], prompt, REWRITE_JSON_SCHEMA, check_rewrite_reply, REWRITE_MODEL,
        call_model, now)
    record = {"unique_prompt_id": share_record["unique_prompt_id"], "corpus_prompt_id": share_record["corpus_prompt_id"],
              "strategy": REWRITE_STRATEGY, "rewrite_model": REWRITE_MODEL, "parse_status": parse_status_of(parsed, raw_replies),
              "problem": None if parsed else problem, "attempts": len(usage_records)}
    for field in ("can_be_rewritten", "what_was_changed", "rewritten_user_message"):
        record[field] = parsed[field] if parsed else None
    add_usage_totals(record, usage_records)
    record.update(raw_replies=raw_replies, rewritten_at=now().isoformat(timespec="seconds"))
    return record, usage_records


def judge_one_rewrite(work_item: dict, call_model, now):
    prompt = render_judge_prompt(work_item["rewritten_user_message"], NO_STAKE_JUDGE_TEMPLATE_VERSION_4_NAME)  # sees the text only
    parsed, raw_replies, usage_records, problem = call_until_parsed(
        STEP_JUDGE, work_item["unique_prompt_id"], prompt, JUDGE_JSON_SCHEMA, check_judge_reply, NO_STAKE_JUDGE_MODEL,
        call_model, now)
    record = {"unique_prompt_id": work_item["unique_prompt_id"], "judge_model": NO_STAKE_JUDGE_MODEL,
              "judge_template_file": NO_STAKE_JUDGE_TEMPLATE_VERSION_4_NAME, "parse_status": parse_status_of(parsed, raw_replies),
              "problem": None if parsed else problem, "attempts": len(usage_records)}
    verdict = parsed or {}
    for key, value in verdict.items():
        record[key] = value
    record["passes_strict"] = passes_strict(parsed) if parsed else None
    record["passes_allowing_background_harm"] = passes_allowing_background_harm(parsed) if parsed else None
    add_usage_totals(record, usage_records)
    record.update(raw_replies=raw_replies, judged_at=now().isoformat(timespec="seconds"))
    return record, usage_records


def label_one_rewrite(work_item: dict, call_model, now):
    prompt = render_animal_role_prompt(work_item["rewritten_user_message"])
    parsed, raw_replies, usage_records, problem = call_until_parsed(
        STEP_LABEL, work_item["unique_prompt_id"], prompt, ANIMAL_ROLE_JSON_SCHEMA, check_animal_role_reply, ANIMAL_ROLE_MODEL,
        call_model, now)
    record = {"unique_prompt_id": work_item["unique_prompt_id"], "label_model": ANIMAL_ROLE_MODEL,
              "label_template_file": ANIMAL_ROLE_TEMPLATE_NAME, "parse_status": parse_status_of(parsed, raw_replies),
              "problem": None if parsed else problem, "attempts": len(usage_records)}
    for key, value in (parsed or {}).items():
        record[key] = value
    if not parsed:
        record["animal_role"] = None
    add_usage_totals(record, usage_records)
    record.update(raw_replies=raw_replies, labelled_at=now().isoformat(timespec="seconds"))
    return record, usage_records


# ---------------------------------------------------------------- work lists

def rewrites_to_make(share_records: list[dict], rewrite_records: list[dict]) -> list[dict]:
    settled = final_record_per_prompt(rewrite_records)
    return [record for record in share_records if record["unique_prompt_id"] not in settled]


def rewrites_to_judge(share_records: list[dict], rewrite_records: list[dict], verdict_records: list[dict]) -> list[dict]:
    """Rewrites that parsed, were not declined and have no settled verdict."""
    rewrites, verdicts = final_record_per_prompt(rewrite_records), final_record_per_prompt(verdict_records)
    return [{"unique_prompt_id": record["unique_prompt_id"],
             "rewritten_user_message": rewrites[record["unique_prompt_id"]]["rewritten_user_message"]}
            for record in share_records
            if record["unique_prompt_id"] in rewrites and rewrites[record["unique_prompt_id"]]["parse_status"] == "ok"
            and rewrites[record["unique_prompt_id"]]["can_be_rewritten"] == "yes"
            and record["unique_prompt_id"] not in verdicts]


def rewrites_to_label(share_records: list[dict], rewrite_records: list[dict], verdict_records: list[dict],
                      label_records: list[dict]) -> list[dict]:
    """Rewrites with an ok verdict (whatever it says) and no settled label."""
    rewrites, verdicts = final_record_per_prompt(rewrite_records), final_record_per_prompt(verdict_records)
    labels = final_record_per_prompt(label_records)
    return [{"unique_prompt_id": record["unique_prompt_id"],
             "rewritten_user_message": rewrites[record["unique_prompt_id"]]["rewritten_user_message"]}
            for record in share_records
            if record["unique_prompt_id"] in verdicts and verdicts[record["unique_prompt_id"]]["parse_status"] == "ok"
            and record["unique_prompt_id"] not in labels]


# ---------------------------------------------------------------- running a step

def run_calls_in_parallel(step_name: str, work_items: list, process_one_item, record_path: Path, usage_path: Path,
                          parallel_calls: int, progress_interval_seconds: float, log) -> dict:
    """Runs process_one_item(item) -> (record, usage_records) on a thread pool; each record and its usage lines are appended and
    flushed as the call finishes. Returns counts, tokens, cost and whether the usage limit stopped the step."""
    result = {"step": step_name, "items_done": 0, "items_failed": 0, "stopped_for_usage_limit": False, "cost_usd": 0.0,
              "tokens": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    if not work_items:
        log(f"{step_name}: nothing to do")
        return result
    write_lock = threading.Lock()
    started = last_report = time.time()
    with open(record_path, "a", encoding="utf-8") as record_file, open(usage_path, "a", encoding="utf-8") as usage_file, \
            ThreadPoolExecutor(max_workers=parallel_calls) as pool:
        futures = [pool.submit(process_one_item, item) for item in work_items]
        for future in as_completed(futures):
            try:
                record, usage_records = future.result()
            except ClaudeCodeUsageLimitReached as error:
                if not result["stopped_for_usage_limit"]:
                    log(f"{step_name}: usage limit reached ({error}); stopping. Run the same command again to resume.")
                result["stopped_for_usage_limit"] = True
                for other in futures:
                    other.cancel()
                continue
            except Exception as error:  # one failed item must not lose the rest; it has no record, so the batch stops before writing
                log(f"{step_name}: one item failed and was left for the rerun: {error!r}")
                result["items_failed"] += 1
                continue
            with write_lock:
                record_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                record_file.flush()
                for usage_record in usage_records:
                    usage_file.write(json.dumps(usage_record) + "\n")
                    result["cost_usd"] += usage_record["cli_reported_cost_usd"] or 0.0
                    for token_field in result["tokens"]:
                        result["tokens"][token_field] += usage_record[token_field]
                usage_file.flush()
            result["items_done"] += 1
            if time.time() - last_report >= progress_interval_seconds:
                last_report = time.time()
                log(f"[{(last_report - started) / 60:.1f} min] {step_name}: {result['items_done']}/{len(work_items)} done, "
                    f"tokens {result['tokens']}, CLI-reported cost so far ${result['cost_usd']:.4f}")
    log(f"{step_name} finished in {(time.time() - started) / 60:.1f} min: {result['items_done']}/{len(work_items)} done, "
        f"CLI-reported cost ${result['cost_usd']:.4f}")
    return result


# ---------------------------------------------------------------- outputs

def build_all_records(share_records: list[dict], rewrite_records: list[dict], verdict_records: list[dict],
                      label_records: list[dict]) -> list[dict]:
    rewrites, verdicts = final_record_per_prompt(rewrite_records), final_record_per_prompt(verdict_records)
    labels = final_record_per_prompt(label_records)
    all_records = []
    for share_record in share_records:
        identifier = share_record["unique_prompt_id"]
        rewrite, verdict, label = rewrites.get(identifier), verdicts.get(identifier), labels.get(identifier)
        record = {field: share_record[field] for field in SHARE_FILE_FIELDS}
        rewrite_ok = bool(rewrite) and rewrite["parse_status"] == "ok"
        verdict_ok = bool(verdict) and verdict["parse_status"] == "ok"
        label_ok = bool(label) and label["parse_status"] == "ok"
        record["rewrite_status"] = rewrite["parse_status"] if rewrite else "not_attempted"
        record["can_be_rewritten"] = rewrite["can_be_rewritten"] if rewrite_ok else None
        record["what_was_changed"] = rewrite["what_was_changed"] if rewrite_ok else None
        record["rewritten_user_message"] = (rewrite["rewritten_user_message"]
                                            if rewrite_ok and rewrite["can_be_rewritten"] == "yes" else None)
        record["no_stake_judge_status"] = (verdict["parse_status"] if verdict else "not_judged")
        record["no_stake_verdict"] = ({key: verdict[key] for key in verdict
                                       if key in JUDGE_ENUM_VALUES_PER_FIELD or key in (
                                           "animals_in_the_message", "harm_from_the_request", "best_answer_ignoring_welfare",
                                           "note", "harm_from_the_request_text_given_although_not_evident")}
                                      if verdict_ok else None)
        record["passes_strict"] = verdict["passes_strict"] if verdict_ok else None
        record["passes_allowing_background_harm"] = verdict["passes_allowing_background_harm"] if verdict_ok else None
        record["animal_role_label_status"] = label["parse_status"] if label else "not_labelled"
        record["animal_role"] = label["animal_role"] if label_ok else None
        record["animal_role_label"] = ({key: label[key] for key in ("animals_in_the_message", "request_in_one_sentence",
                                                                    "what_removing_the_animals_would_lose")}
                                       if label_ok else None)
        if rewrite_ok and rewrite["can_be_rewritten"] == "no":
            outcome = OUTCOME_DECLINED
        elif not rewrite_ok:
            outcome = OUTCOME_REWRITE_FAILED
        elif not verdict_ok:
            outcome = OUTCOME_JUDGE_FAILED
        elif verdict["passes_allowing_background_harm"] is True:
            outcome = OUTCOME_ACCEPTED  # the animal role label is never consulted here
        else:
            outcome = OUTCOME_REJECTED
        record["outcome"] = outcome
        record["accepted"] = outcome == OUTCOME_ACCEPTED
        all_records.append(record)
    return all_records


def build_accepted_controls(all_records: list[dict], batch_number: int, share_number: int) -> list[dict]:
    return [{"corpus_prompt_id": record["corpus_prompt_id"], "unique_prompt_id": record["unique_prompt_id"],
             "spec_id": record["spec_id"], "harm_type": record["harm_type"], "taxa_category": record["taxa_category"],
             "taxa_subcategory": record["taxa_subcategory"], "rewritten_user_message": record["rewritten_user_message"],
             "animal_role": record["animal_role"], "control_rewrite_share_number": share_number,
             "control_rewrite_batch_number": batch_number, "rewrite_strategy": REWRITE_STRATEGY}
            for record in all_records if record["accepted"]]


def usage_totals(usage_path: Path | None) -> dict:
    totals = {"cli_reported_cost_usd": 0.0, "calls": 0, "tokens": {"input_tokens": 0, "output_tokens": 0,
                                                                   "cache_read_input_tokens": 0,
                                                                   "cache_creation_input_tokens": 0}, "by_step": {}}
    for usage in read_jsonl(usage_path) if usage_path else []:
        step = totals["by_step"].setdefault(usage["step"], {"calls": 0, "cli_reported_cost_usd": 0.0, "input_tokens": 0,
                                                            "output_tokens": 0})
        step["calls"] += 1
        step["cli_reported_cost_usd"] += usage["cli_reported_cost_usd"] or 0.0
        step["input_tokens"] += usage["input_tokens"]
        step["output_tokens"] += usage["output_tokens"]
        totals["calls"] += 1
        totals["cli_reported_cost_usd"] += usage["cli_reported_cost_usd"] or 0.0
        for field in totals["tokens"]:
            totals["tokens"][field] += usage[field]
    totals["cli_reported_cost_usd"] = round(totals["cli_reported_cost_usd"], 4)
    for step in totals["by_step"].values():
        step["cli_reported_cost_usd"] = round(step["cli_reported_cost_usd"], 4)
    return totals


def summarise(all_records: list[dict]) -> dict:
    judged = [record for record in all_records if record["no_stake_judge_status"] == "ok"]
    accepted = [record for record in all_records if record["accepted"]]
    by_harm_type: dict[str, dict] = {}
    for record in all_records:
        cell = by_harm_type.setdefault(record["harm_type"], {"prompts": 0, "declined": 0, "judged": 0, "accepted": 0})
        cell["prompts"] += 1
        cell["declined"] += record["outcome"] == OUTCOME_DECLINED
        cell["judged"] += record["no_stake_judge_status"] == "ok"
        cell["accepted"] += record["accepted"]
    return {"prompts_in_share": len(all_records),
            "declined_by_rewriter_count": sum(record["outcome"] == OUTCOME_DECLINED for record in all_records),
            "rewrite_failed_count": sum(record["outcome"] == OUTCOME_REWRITE_FAILED for record in all_records),
            "judged_count": len(judged),
            "judge_failed_count": sum(record["outcome"] == OUTCOME_JUDGE_FAILED for record in all_records),
            "rejected_by_no_stake_judge_count": sum(record["outcome"] == OUTCOME_REJECTED for record in all_records),
            "accepted_count": len(accepted), "by_harm_type": dict(sorted(by_harm_type.items())),
            "animal_role_counts_over_judged_rewrites": {
                role: sum(record["animal_role"] == role for record in judged) for role in ANIMAL_ROLE_VALUES} | {
                "unlabelled": sum(record["animal_role"] is None for record in judged)},
            "animal_role_counts_over_accepted_controls": {
                role: sum(record["animal_role"] == role for record in accepted) for role in ANIMAL_ROLE_VALUES} | {
                "unlabelled": sum(record["animal_role"] is None for record in accepted)}}


# ---------------------------------------------------------------- the batch

def clear_step_markers(batch_folder: Path, step_name: str) -> None:
    for marker_file in (Path(batch_folder) / "step_completion_markers").glob(f"{step_name}_completed_*.json"):
        marker_file.unlink()


def run_control_rewrite_batch(corpus_folder: Path, batch_folder: Path, batch_number: int, started_at: datetime, call_model,
                              parallel_calls: int = PARALLEL_CALLS, now=datetime.now,
                              progress_interval_seconds: float = PROGRESS_REPORT_INTERVAL_SECONDS,
                              prompt_folder: Path = PROMPT_FOLDER) -> dict:
    corpus_folder, batch_folder = extended_length_path(corpus_folder), extended_length_path(batch_folder)
    ledger_path = corpus_folder / LEDGER_FILE_NAME
    started = time.time()
    stamp = now().strftime(TIMESTAMP_FORMAT)
    settings = read_control_settings(corpus_folder)
    share_records = read_verified_share_records(corpus_folder, settings)
    share_number = settings["share_number"]

    def report(message):
        print(f"[{(time.time() - started) / 60:6.1f} min] control rewrite batch {batch_number:03d}: {message}", flush=True)

    if not marker_exists(batch_folder, STEP_SNAPSHOT):
        snapshot_share_and_prompt_files(batch_folder, corpus_folder, settings, stamp, prompt_folder)
        write_marker(batch_folder, STEP_SNAPSHOT, 0)
    else:
        refuse_prompt_files_changed_since_the_batch_started(batch_folder, prompt_folder)
    hashes = snapshot_hashes(batch_folder)
    paths = {prefix: find_existing_file(batch_folder, prefix) or batch_folder / f"{prefix}_{stamp}.jsonl"
             for prefix in (REWRITES_PREFIX, VERDICTS_PREFIX, LABELS_PREFIX, USAGE_PREFIX)}

    def records_of(prefix):
        return read_jsonl(paths[prefix]) if paths[prefix].exists() else []

    def work_for(step_name):
        if step_name == STEP_REWRITE:
            return rewrites_to_make(share_records, records_of(REWRITES_PREFIX))
        if step_name == STEP_JUDGE:
            return rewrites_to_judge(share_records, records_of(REWRITES_PREFIX), records_of(VERDICTS_PREFIX))
        return rewrites_to_label(share_records, records_of(REWRITES_PREFIX), records_of(VERDICTS_PREFIX), records_of(LABELS_PREFIX))

    process_one_item_for_step = {
        STEP_REWRITE: lambda item: rewrite_one_prompt(item, call_model, now),
        STEP_JUDGE: lambda item: judge_one_rewrite(item, call_model, now),
        STEP_LABEL: lambda item: label_one_rewrite(item, call_model, now)}
    for step_name in (STEP_REWRITE, STEP_JUDGE, STEP_LABEL):
        if marker_exists(batch_folder, step_name):
            report(f"{step_name}: already complete, skipping")
            continue
        step_started = time.time()
        items = work_for(step_name)
        report(f"{step_name}: starting with {len(items)} items of {len(share_records)} prompts")
        for round_number in range(1, MAXIMUM_RECORDS_OF_FAILURE_BEFORE_GIVING_UP + 1):
            # A prompt whose calls failed is tried again here, so after this loop every prompt is settled (an ok result, or
            # failed in the maximum number of rounds) unless an item raised an unexpected error and left no record.
            if not items:
                break
            result = run_calls_in_parallel(step_name, items, process_one_item_for_step[step_name],
                                           paths[STEP_PREFIXES[step_name]], paths[USAGE_PREFIX], parallel_calls,
                                           progress_interval_seconds, report)
            if result["stopped_for_usage_limit"]:
                raise BatchStoppedForUsageLimit(
                    f"{step_name} stopped because the Claude subscription usage limit was reached. Nothing is lost: wait for "
                    f"the usage window to reset, then run the same command again to resume {batch_folder}")
            items = work_for(step_name)
            if items and round_number < MAXIMUM_RECORDS_OF_FAILURE_BEFORE_GIVING_UP:
                report(f"{step_name}: {len(items)} prompts had no usable reply; trying them again (round {round_number + 1})")
        if items:
            raise BatchFailure(f"{step_name}: {len(items)} of {len(share_records)} prompts have no settled result (a call "
                               f"raised an unexpected error and left no record). Nothing was written to the accepted file, "
                               f"summary or ledger. Run the same command again to resume.")
        write_marker(batch_folder, step_name, time.time() - step_started)
        report(f"{step_name}: done in {(time.time() - step_started) / 60:.1f} minutes")

    for step_name in (STEP_REWRITE, STEP_JUDGE, STEP_LABEL):  # the rule of the batch script: no settled result, no outputs
        if work_for(step_name):
            clear_step_markers(batch_folder, step_name)
            raise BatchFailure(f"{step_name} has prompts without a settled result, so nothing was written. Run the same "
                               f"command again to resume.")

    report("building the all records file, the accepted controls and the summary")
    all_records = build_all_records(share_records, records_of(REWRITES_PREFIX), records_of(VERDICTS_PREFIX), records_of(LABELS_PREFIX))
    accepted_controls = build_accepted_controls(all_records, batch_number, share_number)
    final_stamp = now().strftime(TIMESTAMP_FORMAT)
    all_records_path = batch_folder / f"control_rewrite_all_records_batch_{batch_number:03d}_{final_stamp}.jsonl"
    all_records_path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in all_records), encoding="utf-8")
    accepted_path = batch_folder / f"accepted_control_prompts_batch_{batch_number:03d}_{final_stamp}.jsonl"
    accepted_path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in accepted_controls),
                             encoding="utf-8")
    counts = summarise(all_records)
    costs = usage_totals(paths[USAGE_PREFIX])
    models = {"rewrite_model": REWRITE_MODEL, "no_stake_judge_model": NO_STAKE_JUDGE_MODEL, "animal_role_model": ANIMAL_ROLE_MODEL}
    summary = {"batch_number": batch_number, "share_number": share_number, "rewrite_strategy": REWRITE_STRATEGY, **models,
               **counts, "prompt_file_sha256": hashes["prompt_files"], "share_file_sha256": hashes["share_file"],
               "started_at": started_at.isoformat(timespec="seconds"), "ended_at": now().isoformat(timespec="seconds"),
               "costs": costs, "all_records_file": all_records_path.name, "accepted_file": accepted_path.name}
    (batch_folder / f"control_rewrite_batch_summary_{final_stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    ledger_entry = {"batch_number": batch_number, "batch_folder": ledger_batch_folder_text(batch_folder, corpus_folder),
                    "batch_kind": "control_rewrite", "share_number": share_number,
                    "prompts_in_share": counts["prompts_in_share"], "declined_by_rewriter_count": counts["declined_by_rewriter_count"],
                    "judged_count": counts["judged_count"], "accepted_count": counts["accepted_count"],
                    "accepted_file": accepted_path.name, "all_records_file": all_records_path.name, **models,
                    "prompt_file_sha256": hashes["prompt_files"], "share_file_sha256": hashes["share_file"],
                    "started_at": summary["started_at"], "ended_at": summary["ended_at"], "costs": costs}
    append_ledger_line(ledger_path, ledger_entry)
    report(f"ledger updated: {counts['accepted_count']} accepted of {counts['prompts_in_share']} prompts "
           f"({counts['declined_by_rewriter_count']} declined, {counts['judged_count']} judged, CLI-reported cost "
           f"${costs['cli_reported_cost_usd']:.4f})")
    return ledger_entry


def print_plan(corpus_folder: Path, share_records: list[dict], settings: dict) -> None:
    print(f"Corpus folder: {corpus_folder}\nShare number: {settings['share_number']}, {len(share_records)} prompts "
          f"(share file sha256 {settings['share_file_sha256']})")
    print(f"Models: rewrite {REWRITE_MODEL}, no-stake judge {NO_STAKE_JUDGE_MODEL}, animal role label {ANIMAL_ROLE_MODEL}")
    for name, digest in prompt_file_hashes().items():
        print(f"prompt file {name}: sha256 {digest}")
    print(f"Calls: at most {len(share_records)} rewrites, then one no-stake judge and one animal role label for each rewrite the "
          f"model did not decline (about 3 per prompt); retries add more (up to {MAXIMUM_ATTEMPTS_PER_CALL} attempts per call).")
    if share_records:
        print(f"\n----- rendered rewrite prompt for {share_records[0]['unique_prompt_id']} -----")
        print(render_rewrite_prompt(share_records[0], REWRITE_STRATEGY))
    print(f"\nAfter success, one line is appended to {Path(corpus_folder) / LEDGER_FILE_NAME}. Nothing is created or run.")


def main(argv=None, call_model=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-folder", type=Path, required=True)
    parser.add_argument("--resume-batch-folder", type=Path, default=None, help="finish a batch folder that has no ledger entry yet")
    parser.add_argument("--parallel-calls", type=int, default=PARALLEL_CALLS)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and one rendered prompt; create nothing")
    arguments = parser.parse_args(argv)
    # Windows extended-length paths (no effect elsewhere); resolve can drop the prefix, so add it after.
    corpus_folder = extended_length_path(arguments.corpus_folder.resolve())
    try:
        settings = read_control_settings(corpus_folder)
        share_records = read_verified_share_records(corpus_folder, settings)
        if arguments.dry_run:
            print_plan(corpus_folder, share_records, settings)
            return 0
        if arguments.resume_batch_folder is not None:
            batch_folder = extended_length_path(arguments.resume_batch_folder.resolve())
            match = BATCH_FOLDER_PATTERN.match(batch_folder.name)
            if not match or not batch_folder.is_dir():
                parser.error(f"{batch_folder} is not a batch folder named batch_<NNN>_control_rewrite_share_<NNN>_<stamp>")
            batch_number = int(match.group(1))
            if batch_number in batch_numbers_in_ledger(corpus_folder / LEDGER_FILE_NAME):
                parser.error(f"batch {batch_number} is already in the ledger; nothing to resume")
        else:
            incomplete = incomplete_batch_folders(corpus_folder)
            if incomplete:
                print("Refusing to start a new batch: these batch folders have no ledger entry:", file=sys.stderr)
                for folder in incomplete:
                    print(f"  {remove_extended_length_prefixes_from_text(str(folder))}", file=sys.stderr)
                print("Resume one with --resume-batch-folder <folder>.", file=sys.stderr)
                return 1
            if read_ledger(corpus_folder / LEDGER_FILE_NAME):
                print("This share's batch is already in the ledger; nothing to run.", file=sys.stderr)
                return 1
            batch_number = next_batch_number(corpus_folder)
            batch_folder = corpus_folder / "batches" / batch_folder_name(batch_number, settings["share_number"],
                                                                         datetime.now().strftime(TIMESTAMP_FORMAT))
        if call_model is None:
            from claude_code_cli_structured_reply_without_paid_api_fallback import call_claude_code_cli_for_structured_reply

            def call_model(model, prompt, json_schema):
                return call_claude_code_cli_for_structured_reply(model, prompt, json_schema)
        batch_folder.mkdir(parents=True, exist_ok=True)
        run_control_rewrite_batch(corpus_folder, batch_folder, batch_number, datetime.now(), call_model,
                                  parallel_calls=arguments.parallel_calls)
    except BatchStoppedForUsageLimit as failure:
        print(remove_extended_length_prefixes_from_text(f"STOPPED FOR USAGE LIMIT: {failure}"), file=sys.stderr)
        return USAGE_LIMIT_EXIT_CODE
    except BatchFailure as failure:
        print(remove_extended_length_prefixes_from_text(f"FAILED: {failure}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
