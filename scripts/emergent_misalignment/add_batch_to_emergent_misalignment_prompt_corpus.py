"""Add one batch of prompts to the append-only emergent misalignment prompt corpus.

Each run of this script generates one batch: it deals new scenarios (each drawing its spec from the corpus's fixed spec
set, see below), drafts and gates prompts (pipeline step 1), keeps
the implicit gate-passed ones, judges each for answerability without welfare and for an evident and decisive welfare
stake, and keeps the prompts that are both answerable and useful. The corpus folder only ever grows; see
README_how_this_corpus_grows.md in it.

Corpus settings: a corpus folder may hold corpus_settings_scenario_seed_base_and_spec_set_file.json naming the scenario
seed base and the spec set file (relative to the corpus folder). A folder without it keeps the old behaviour: seed base
20270000 and no spec set (scenarios keep the harm type, practical advantage and subcategory they were dealt).

Steps (each leaves a completion marker in the batch folder, so --resume-batch-folder skips finished ones):
  snapshot prompts -> write config -> pipeline step 1 -> selection -> answerability judge (twice) ->
  usefulness judge (twice) -> accepted file -> summary -> ledger line (last)
The judges are run twice because a call blocked by safety filters is left for the second run to retry.

Two modes. Default: every deal draws a spec that has never been dealt in this corpus, whatever the outcome of its earlier
attempts, and that is not reserved for a contributor share (reserved_spec_ids_for_contributor_shares_*.json in the
corpus folder; reserved specs count as dealt for the dealer, so they are never drawn), until a category has none left. --retry-failed-specs: the batch deals exactly the specs whose current status is
failed (fewest attempts first, --deals caps how many, --retry-failure-stages filters), regenerated and rescored from scratch
with no feedback from the earlier failure. Each finished batch writes a spec outcomes file and a new spec status table (see
spec_outcomes_and_status_table_for_prompt_corpus.py).

Everything goes through the Claude Code CLI (subscription), via the pipeline and the two judge scripts. This script makes
no model call itself. Use --dry-run to print the plan and create nothing.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent.parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))
from select_implicit_prompts_from_difficult_advice_step_one_run import select_prompts  # noqa: E402
from load_pipeline_scenario_dealer import load_scenario_dealer  # noqa: E402
from reserved_spec_ids_for_contributor_shares import read_reserved_spec_ids  # noqa: E402
from windows_extended_length_paths import (  # noqa: E402
    extended_length_path, path_without_extended_length_prefix, relative_path_text_under_folder,
    remove_extended_length_prefixes_from_text)
from spec_outcomes_and_status_table_for_prompt_corpus import (  # noqa: E402
    ANSWERABILITY_VERDICT_PATTERN, FAILURE_STAGES, LEDGER_FILE_NAME, USEFULNESS_VERDICT_PATTERN,
    build_spec_status_table, find_pipeline_run_folder, newest_file, ok_verdicts_in_folder, outcome_counts,
    prompt_ids_with_no_verdict_record_in_folder,
    write_batch_spec_outcomes_file, write_spec_status_table)

# This repository's own virtual environment when it exists; otherwise (the contributor kit, or any other checkout) the
# Python that runs this script, so the pipeline and judges use the same installed packages.
_REPOSITORY_VIRTUAL_ENVIRONMENT_PYTHON = REPOSITORY_ROOT / "animal-welfare-data-pipeline" / ".venv" / "bin" / "python"
PYTHON_EXECUTABLE = (str(_REPOSITORY_VIRTUAL_ENVIRONMENT_PYTHON) if _REPOSITORY_VIRTUAL_ENVIRONMENT_PYTHON.exists()
                     else sys.executable)
USAGE_LIMIT_EXIT_CODE = 3  # the judges exit with this when the subscription usage limit stops them
USAGE_LIMIT_TEXT_MARKERS = ("usage limit", "spend limit", "hit your limit", "limit · resets")  # in a step's terminal output: the pipeline's usage-limit error
DEFAULT_CORPUS_FOLDER = (REPOSITORY_ROOT / "data"
                         / "emergent_misalignment_prompt_corpus_category_subcategory_harm_and_advantage_specs")
CORPUS_SETTINGS_FILE_NAME = "corpus_settings_scenario_seed_base_and_spec_set_file.json"
PIPELINE_FOLDER = REPOSITORY_ROOT / "strictly_implicit_difficult_advice_pipeline_copy"
GENERATION_PROMPT_FOLDER = PIPELINE_FOLDER / "prompts" / "dad"
GENERATION_PROMPT_FILE_NAMES = ["step1a_scenario.txt", "step1b_dilemmas.txt", "step1c_gate.txt", "step1d_refine.txt",
                                "variables.txt"]
# The species table (with the relative weights of narrow subcategories) shapes what scenarios are dealt, so it is
# snapshotted next to the prompts and counts toward the generation prompt version hash.
GENERATION_PROFILE_FOLDER = PIPELINE_FOLDER / "dad_pipeline" / "profiles"
GENERATION_PROFILE_FILE_NAME = "animal.py"
# The scenario dealer (including the draw of specs from the fixed spec set) decides which scenarios are dealt, so it is
# snapshotted and hashed the same way. The spec set file is hashed with the generation prompts (see
# generation_prompt_version_hash), and its full sha256 is also recorded in every batch summary and ledger line.
GENERATION_DEALER_FOLDER = PIPELINE_FOLDER / "dad_pipeline"
GENERATION_DEALER_FILE_NAME = "compose_scenarios.py"
JUDGE_PROMPT_FOLDER = SCRIPT_DIRECTORY / "prompts"
ANSWERABILITY_JUDGE_TEMPLATE_NAME = \
    "judge_whether_a_complete_answer_is_possible_without_mentioning_animal_welfare_prompt_template.txt"
USEFULNESS_JUDGE_TEMPLATE_NAME = \
    "judge_whether_prompt_carries_an_evident_and_decisive_animal_welfare_stake_prompt_template.txt"
ANSWERABILITY_JUDGE_SCRIPT = SCRIPT_DIRECTORY / \
    "judge_whether_a_complete_answer_is_possible_without_mentioning_animal_welfare_through_claude_code_cli.py"
USEFULNESS_JUDGE_SCRIPT = SCRIPT_DIRECTORY / \
    "judge_whether_prompt_carries_an_evident_and_decisive_animal_welfare_stake_through_claude_code_cli.py"
SELECTION_SCRIPT = SCRIPT_DIRECTORY / "select_implicit_prompts_from_difficult_advice_step_one_run.py"
CONFIG_TEMPLATE_PATH = SCRIPT_DIRECTORY / "prompt_corpus_batch_pipeline_config_template.yaml"
SPEC_DEALING_PLAN_FILE_PATTERN = "spec_dealing_plan_for_batch_*.json"
SPEC_SET_CONFIG_KEY = "spec_set_path"
SPEC_DEALING_PLAN_CONFIG_KEY = "spec_dealing_plan_path"
MODE_DEFAULT = "default_never_dealt_specs"
MODE_RETRY = "retry_failed_specs"
SCENARIO_SEED_BASE = 20270000  # the seed base of a corpus folder with no settings file (the retired first corpus)
SELECTION_COUNT_LARGE_ENOUGH_TO_KEEP_EVERYTHING = 100000
PROGRESS_REPORT_INTERVAL_SECONDS = 180
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"
BATCH_FOLDER_PATTERN = re.compile(r"^batch_(\d{3,})_seed_(\d+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")


# ---------- numbering, seeds, hashes ----------

def read_ledger(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    with open(ledger_path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def batch_numbers_in_ledger(ledger_path: Path) -> set[int]:
    return {int(entry["batch_number"]) for entry in read_ledger(ledger_path)}


def batch_folders_present(corpus_folder: Path) -> dict[int, Path]:
    batches_folder = corpus_folder / "batches"
    found = {}
    if batches_folder.exists():
        for folder in sorted(batches_folder.iterdir()):
            match = BATCH_FOLDER_PATTERN.match(folder.name)
            if folder.is_dir() and match:
                found[int(match.group(1))] = folder
    return found


def next_batch_number(corpus_folder: Path) -> int:
    """One more than the highest number in the ledger or in the batch folder names, so an incomplete batch is never reused."""
    known_numbers = batch_numbers_in_ledger(corpus_folder / LEDGER_FILE_NAME) | set(batch_folders_present(corpus_folder))
    return max(known_numbers, default=0) + 1


def incomplete_batch_folders(corpus_folder: Path) -> list[Path]:
    completed = batch_numbers_in_ledger(corpus_folder / LEDGER_FILE_NAME)
    return [folder for number, folder in batch_folders_present(corpus_folder).items() if number not in completed]


def read_corpus_settings(corpus_folder: Path) -> dict:
    """The corpus's scenario seed base and spec set file name; a folder with no settings file keeps the old behaviour."""
    path = Path(corpus_folder) / CORPUS_SETTINGS_FILE_NAME
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"scenario_seed_base": settings.get("scenario_seed_base", SCENARIO_SEED_BASE),
            "spec_set_file": settings.get("spec_set_file")}


def spec_set_path_for_corpus(corpus_folder: Path) -> Path | None:
    name = read_corpus_settings(corpus_folder)["spec_set_file"]
    if name is None:
        return None
    path = Path(corpus_folder) / name
    if not path.exists():
        raise BatchFailure(f"the corpus settings name the spec set file {path}, which does not exist")
    return path


def scenario_seed_for_batch(batch_number: int, seed_base: int = SCENARIO_SEED_BASE) -> int:
    return seed_base + batch_number


def hash_files(file_paths: list[Path]) -> str:
    """sha256 over the contents of the files, in order of file name; first 12 hex characters."""
    digest = hashlib.sha256()
    for path in sorted(file_paths, key=lambda p: p.name):
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:12]


def generation_prompt_version_hash(prompt_folder: Path = GENERATION_PROMPT_FOLDER,
                                   profile_folder: Path = GENERATION_PROFILE_FOLDER,
                                   dealer_folder: Path = GENERATION_DEALER_FOLDER,
                                   spec_set_path: Path | None = None) -> str:
    """Hash of the generation prompts, species table, dealer and (when the corpus has one) the spec set file."""
    return hash_files([prompt_folder / name for name in GENERATION_PROMPT_FILE_NAMES]
                      + [profile_folder / GENERATION_PROFILE_FILE_NAME,
                         dealer_folder / GENERATION_DEALER_FILE_NAME]
                      + ([spec_set_path] if spec_set_path else []))


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def judge_prompt_hashes(judge_prompt_folder: Path = JUDGE_PROMPT_FOLDER) -> dict[str, str]:
    return {"answerability_judge_prompt_hash": hash_files([judge_prompt_folder / ANSWERABILITY_JUDGE_TEMPLATE_NAME]),
            "usefulness_judge_prompt_hash": hash_files([judge_prompt_folder / USEFULNESS_JUDGE_TEMPLATE_NAME])}


def batch_folder_name(batch_number: int, seed: int, stamp: str) -> str:
    return f"batch_{batch_number:03d}_seed_{seed}_{stamp}"


# ---------- files for one batch ----------

def snapshot_prompts(batch_folder: Path, generation_prompt_folder: Path = GENERATION_PROMPT_FOLDER,
                     judge_prompt_folder: Path = JUDGE_PROMPT_FOLDER,
                     generation_profile_folder: Path = GENERATION_PROFILE_FOLDER,
                     generation_dealer_folder: Path = GENERATION_DEALER_FOLDER,
                     spec_set_path: Path | None = None) -> None:
    destination = batch_folder / "generation_and_judge_prompts_used_for_this_batch"
    destination.mkdir(parents=True, exist_ok=True)
    for name in GENERATION_PROMPT_FILE_NAMES:
        shutil.copy2(generation_prompt_folder / name, destination / name)
    shutil.copy2(generation_profile_folder / GENERATION_PROFILE_FILE_NAME, destination / GENERATION_PROFILE_FILE_NAME)
    shutil.copy2(generation_dealer_folder / GENERATION_DEALER_FILE_NAME, destination / GENERATION_DEALER_FILE_NAME)
    for name in (ANSWERABILITY_JUDGE_TEMPLATE_NAME, USEFULNESS_JUDGE_TEMPLATE_NAME):
        shutil.copy2(judge_prompt_folder / name, destination / name)
    if spec_set_path is not None:
        shutil.copy2(spec_set_path, destination / Path(spec_set_path).name)


def plan_spec_dealing(corpus_folder: Path, batch_number: int, deals: int, retry_failed_specs: bool = False,
                      retry_failure_stages: list[str] | None = None) -> dict:
    """What the batch deals, from the spec status table of the batches before it. Every spec dealt before, whatever its
    outcome, is in dealt_spec_ids and is never drawn again in a default batch. A retry batch also lists forced_spec_ids:
    the failed specs (latest failure stage in retry_failure_stages when given), fewest attempts first, at most `deals`.
    Raises BatchFailure when a retry batch has nothing to retry."""
    spec_set_path = spec_set_path_for_corpus(corpus_folder)
    if spec_set_path is None:
        raise BatchFailure("this corpus has no spec set, so there is no spec status to plan from")
    spec_rows = read_jsonl(spec_set_path)
    table = build_spec_status_table(corpus_folder, spec_rows, before_batch_number=batch_number)
    truly_dealt_spec_ids = {row["spec_id"] for row in table if row["attempts"] > 0}
    reserved_spec_ids = read_reserved_spec_ids(corpus_folder) & {row["spec_id"] for row in spec_rows}
    # The dealer reads only dealt_spec_ids and never draws one of them, so reserved specs are listed there too.
    plan = {"mode": MODE_RETRY if retry_failed_specs else MODE_DEFAULT,
            "dealt_spec_ids": sorted(truly_dealt_spec_ids | reserved_spec_ids),
            "reserved_spec_ids": sorted(reserved_spec_ids - truly_dealt_spec_ids),
            "forced_spec_ids": None, "attempts_so_far": {}, "retry_failure_stages": retry_failure_stages}
    if retry_failed_specs:
        candidates = [row for row in table if row["status"] == "failed"
                      and (not retry_failure_stages or row["latest_failure_stage"] in retry_failure_stages)]
        chosen = sorted(candidates, key=lambda row: (row["attempts"], row["spec_id"]))[:deals]
        if not chosen:
            raise BatchFailure("there is nothing to retry: no spec has status failed"
                               + (f" with a latest failure stage in {retry_failure_stages}" if retry_failure_stages else ""))
        plan["forced_spec_ids"] = [row["spec_id"] for row in chosen]
        plan["attempts_so_far"] = {row["spec_id"]: row["attempts"] for row in chosen}
        plan["retried_specs"] = [{"spec_id": row["spec_id"], "attempts_so_far": row["attempts"],
                                  "latest_failure_stage": row["latest_failure_stage"],
                                  "taxa_category": row["taxa_category"], "taxa_subcategory": row["taxa_subcategory"]}
                                 for row in chosen]
    elif retry_failure_stages:
        raise BatchFailure("--retry-failure-stages only applies with --retry-failed-specs")
    plan["failed_spec_count"] = sum(row["status"] == "failed" for row in table)
    plan["failed_spec_count_by_category"] = {}
    for row in table:
        if row["status"] == "failed":
            plan["failed_spec_count_by_category"][row["taxa_category"]] = \
                plan["failed_spec_count_by_category"].get(row["taxa_category"], 0) + 1
    return plan


def spec_attempt_numbers_of_plan(plan: dict | None) -> dict[str, int]:
    """The attempt number each forced spec is on in this batch (a spec dealt for the first time is attempt 1)."""
    return {spec_id: attempts + 1 for spec_id, attempts in ((plan or {}).get("attempts_so_far") or {}).items()}


def preflight_deal(plan: dict, corpus_folder: Path, scenario_seed: int, deals: int) -> None:
    """Deal the batch in memory, exactly as pipeline step 1 will (same seed, spec set and plan), so that a category
    with no never-dealt spec left fails here, before any generation, naming the category and the failed specs that
    could be retried."""
    import random
    import tempfile
    compose_scenarios = load_scenario_dealer()
    spec_set_path = spec_set_path_for_corpus(corpus_folder)
    count = len(plan["forced_spec_ids"]) if plan["forced_spec_ids"] else deals
    with tempfile.TemporaryDirectory() as temporary_folder:
        plan_path = Path(temporary_folder) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        try:
            compose_scenarios.deal_scenarios(count, random.Random(scenario_seed), compose_scenarios.DEFAULT_VARIABLES,
                                             spec_set_path, plan_path)
        except compose_scenarios.SpecPoolExhausted as error:
            in_category = plan["failed_spec_count_by_category"].get(error.category, 0)
            raise BatchFailure(
                f"{error} ({len(plan.get('reserved_spec_ids', []))} specs are reserved for contributor shares and count "
                f"as dealt here; release shares with a newer reserved_spec_ids_for_contributor_shares file.) "
                f"{plan['failed_spec_count']} failed specs ({in_category} in this category) could be retried "
                f"with --retry-failed-specs. No batch folder was created.") from error


def write_spec_dealing_plan_file(batch_folder: Path, plan: dict, batch_number: int, stamp: str) -> Path:
    """Write (once) the batch's spec dealing plan. A file already in the batch folder is kept, so a resumed batch deals
    the same specs and never counts itself."""
    existing = newest_file(batch_folder, SPEC_DEALING_PLAN_FILE_PATTERN)
    if existing is not None:
        return existing
    path = batch_folder / f"spec_dealing_plan_for_batch_{batch_number:03d}_{stamp}.json"
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def write_batch_config(batch_folder: Path, seed: int, deals: int, stamp: str,
                       template_path: Path = CONFIG_TEMPLATE_PATH, spec_set_path: Path | None = None,
                       spec_dealing_plan_path: Path | None = None) -> Path:
    config = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    config["dad"]["dilemmas"]["scenario_seed"] = seed
    config["dad"]["dilemmas"]["count"] = deals
    # The config is an output file, so it never holds the Windows extended-length prefix; the pipeline adds it again when it
    # reads these two paths (shared.utils.extended_length_path).
    config["dad"]["dilemmas"][SPEC_SET_CONFIG_KEY] = str(path_without_extended_length_prefix(spec_set_path)) if spec_set_path else None
    config["dad"]["dilemmas"][SPEC_DEALING_PLAN_CONFIG_KEY] = (
        str(path_without_extended_length_prefix(spec_dealing_plan_path)) if spec_dealing_plan_path else None)
    header = (f"# Pipeline configuration for one batch of the emergent misalignment prompt corpus, written from\n"
              f"# {template_path.name}: scenario seed {seed}, {deals} deals, spec set "
              f"{spec_set_path.name if spec_set_path else 'none'}, spec dealing plan "
              f"{spec_dealing_plan_path.name if spec_dealing_plan_path else 'none'}.\n")
    config_path = batch_folder / f"pipeline_config_for_this_batch_{stamp}.yaml"
    config_path.write_text(header + yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def marker_exists(batch_folder: Path, step_name: str) -> bool:
    return newest_file(batch_folder / "step_completion_markers", f"{step_name}_completed_*.json") is not None


GENERATION_TIMES_STATE_FILE_NAME = "batch_generation_times_state.json"


def read_generation_times_state(batch_folder: Path) -> dict:
    path = batch_folder / GENERATION_TIMES_STATE_FILE_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def update_generation_times_state(batch_folder: Path, field: str, value: str, keep_existing: bool) -> None:
    """Record a generation time; with keep_existing, an earlier value (from before a resume) is never overwritten."""
    state = read_generation_times_state(batch_folder)
    if keep_existing and field in state:
        return
    state[field] = value
    (batch_folder / GENERATION_TIMES_STATE_FILE_NAME).write_text(json.dumps(state, indent=2), encoding="utf-8")


def step_stopped_for_usage_limit(return_code: int, terminal_output_path: Path) -> bool:
    """The judges exit with USAGE_LIMIT_EXIT_CODE; the pipeline crashes with a usage-limit error in its output."""
    if return_code == USAGE_LIMIT_EXIT_CODE:
        return True
    try:
        output = Path(terminal_output_path).read_text(encoding="utf-8", errors="replace")[-20000:].lower()
    except OSError:
        return False
    return any(marker in output for marker in USAGE_LIMIT_TEXT_MARKERS)


def write_marker(batch_folder: Path, step_name: str, seconds_taken: float) -> None:
    folder = batch_folder / "step_completion_markers"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    (folder / f"{step_name}_completed_{stamp}.json").write_text(
        json.dumps({"step": step_name, "seconds_taken": round(seconds_taken, 1)}), encoding="utf-8")


# ---------- commands ----------

def pipeline_command(config_path: Path, batch_number: int, resume_run_id: str | None = None) -> list[str]:
    command = [PYTHON_EXECUTABLE, "-m", "dad_pipeline.run", "--config", str(config_path), "--step", "1",
               "--stop-after", "1", "--label", f"prompt-corpus-batch-{batch_number:03d}"]
    if resume_run_id:
        command += ["--resume", "--run-id", resume_run_id]
    return command


def selection_command(step_one_folder: Path, batch_folder: Path) -> list[str]:
    return [PYTHON_EXECUTABLE, str(SELECTION_SCRIPT), "--step-one-folder", str(step_one_folder),
            "--output-folder", str(batch_folder), "--count", str(SELECTION_COUNT_LARGE_ENOUGH_TO_KEEP_EVERYTHING)]


def judge_command(judge_script: Path, prompts_file: Path, output_folder: Path) -> list[str]:
    return [PYTHON_EXECUTABLE, str(judge_script), "--prompts-file", str(prompts_file), "--output-folder", str(output_folder)]


def run_command(command: list[str], terminal_output_path: Path, working_folder: Path | None = None,
                environment_additions: dict | None = None) -> int:
    """Run a subprocess, appending its output to the terminal output file; report liveness about every three minutes."""
    import os
    environment = dict(os.environ)
    environment.update(environment_additions or {})
    started = time.time()
    with open(terminal_output_path, "a", encoding="utf-8") as log_handle:
        log_handle.write(remove_extended_length_prefixes_from_text(f"$ {' '.join(command)}") + "\n")
        log_handle.flush()
        process = subprocess.Popen(command, cwd=working_folder, env=environment, stdout=log_handle,
                                   stderr=subprocess.STDOUT)
        while True:
            try:
                return process.wait(timeout=PROGRESS_REPORT_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                print(f"    still running after {(time.time() - started) / 60:.1f} minutes: {terminal_output_path.name}",
                      flush=True)


# ---------- verdicts and accepted records ----------

def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_accepted_records(prompt_records: list[dict], answerability_verdicts: dict[str, dict],
                           usefulness_verdicts: dict[str, dict], batch_number: int, scenario_seed: int,
                           prompt_version_hash: str, generation_started_at: str | None = None,
                           generation_finished_at: str | None = None,
                           spec_attempt_numbers: dict[str, int] | None = None) -> tuple[list[dict], dict]:
    """Accepted = answerable yes AND useful true. Prompts with a missing ok verdict from either judge are unjudged."""
    accepted, unjudged_ids = [], []
    counts = {"selected": len(prompt_records), "answerable": 0, "useful": 0, "accepted": 0}
    for record in prompt_records:
        gid = record["prompt_gid"]
        answerability = answerability_verdicts.get(gid)
        usefulness = usefulness_verdicts.get(gid)
        if answerability is None or usefulness is None:
            unjudged_ids.append(gid)
        is_answerable = bool(answerability) and answerability["complete_answer_without_welfare_possible"] == "yes"
        is_useful = bool(usefulness) and usefulness["useful"] is True
        counts["answerable"] += is_answerable
        counts["useful"] += is_useful
        if is_answerable and is_useful:
            accepted.append({**record,
                             "corpus_prompt_id": f"B{batch_number:03d}-{gid}",
                             "corpus_batch_number": batch_number,
                             "scenario_seed": scenario_seed,
                             "spec_attempt_number": ((spec_attempt_numbers or {}).get(record["spec_id"], 1)
                                                     if record.get("spec_id") else None),
                             "generation_prompt_version_hash": prompt_version_hash,
                             "batch_generation_started_at": generation_started_at,
                             "batch_generation_finished_at": generation_finished_at,
                             "answerability_note": answerability.get("note"),
                             "usefulness_note": usefulness.get("note"),
                             "best_answer_ignoring_welfare": usefulness.get("best_answer_ignoring_welfare")})
    counts["accepted"] = len(accepted)
    counts["unjudged_prompt_ids"] = unjudged_ids
    counts["unjudged"] = len(unjudged_ids)
    return accepted, counts


def sum_cost(paths: list[Path], field: str) -> float:
    total = 0.0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total += json.loads(line).get(field) or 0.0
    return round(total, 4)


def cost_totals(batch_folder: Path) -> dict:
    pipeline_run = find_pipeline_run_folder(batch_folder)
    pipeline_logs = [pipeline_run / "cost_log.jsonl"] if pipeline_run and (pipeline_run / "cost_log.jsonl").exists() else []
    return {"pipeline_cost_usd": sum_cost(pipeline_logs, "cost_usd"),
            "answerability_judge_cli_reported_cost_usd": sum_cost(
                list((batch_folder / "answerability_judge").glob("*usage_log_*.jsonl")), "cli_reported_cost_usd"),
            "usefulness_judge_cli_reported_cost_usd": sum_cost(
                list((batch_folder / "usefulness_judge").glob("*usage_log_*.jsonl")), "cli_reported_cost_usd")}


def append_ledger_line(ledger_path: Path, entry: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- the batch ----------

def ledger_batch_folder_text(batch_folder: Path, corpus_folder: Path) -> str:
    """The batch folder relative to the corpus folder, as it is written in the ledger: never with a Windows extended-length
    prefix, and always with '/' (the merge reads it on any operating system)."""
    relative_text = relative_path_text_under_folder(batch_folder, corpus_folder)
    return Path(relative_text).as_posix() if relative_text else batch_folder.relative_to(corpus_folder).as_posix()


class BatchFailure(Exception):
    pass


class BatchStoppedForUsageLimit(BatchFailure):
    """A step stopped because the subscription usage limit was reached; the batch resumes later from its checkpoints."""


def run_batch(corpus_folder: Path, batch_folder: Path, batch_number: int, scenario_seed: int, deals: int,
              started_at: datetime, run_command_function=run_command, now=datetime.now,
              retry_failed_specs: bool = False, retry_failure_stages: list[str] | None = None) -> dict:
    # Entry points convert their paths already; converting again here keeps a direct caller safe too (no-op off Windows).
    corpus_folder, batch_folder = extended_length_path(corpus_folder), extended_length_path(batch_folder)
    ledger_path = corpus_folder / LEDGER_FILE_NAME
    started = time.time()
    stamp = now().strftime(TIMESTAMP_FORMAT)

    def report(message):
        print(f"[{(time.time() - started) / 60:6.1f} min] batch {batch_number:03d}: {message}", flush=True)

    def run_step(step_name, command, working_folder=None, environment_additions=None):
        if marker_exists(batch_folder, step_name):
            report(f"{step_name}: already complete, skipping")
            return
        step_started = time.time()
        if step_name == "pipeline_step_1":
            update_generation_times_state(batch_folder, "batch_generation_started_at",
                                          now().astimezone().isoformat(timespec="seconds"), keep_existing=True)
        report(f"{step_name}: starting")
        terminal_output_path = batch_folder / f"terminal_output_{step_name}_{now().strftime(TIMESTAMP_FORMAT)}.txt"
        return_code = run_command_function(command, terminal_output_path, working_folder, environment_additions)
        if return_code != 0 and step_stopped_for_usage_limit(return_code, terminal_output_path):
            raise BatchStoppedForUsageLimit(
                f"{step_name} stopped because the Claude subscription usage limit was reached. Nothing is lost: wait for "
                f"the usage window to reset, then resume with --resume-batch-folder {batch_folder}")
        if return_code != 0:
            raise BatchFailure(f"{step_name} exited with code {return_code}; see {terminal_output_path}. "
                               f"Resume with --resume-batch-folder {batch_folder}")
        write_marker(batch_folder, step_name, time.time() - step_started)
        if step_name == "pipeline_step_1":
            update_generation_times_state(batch_folder, "batch_generation_finished_at",
                                          now().astimezone().isoformat(timespec="seconds"), keep_existing=False)
        report(f"{step_name}: done in {(time.time() - step_started) / 60:.1f} minutes")

    # snapshot prompts and config
    spec_set_path = spec_set_path_for_corpus(corpus_folder)
    if not marker_exists(batch_folder, "snapshot_prompts_and_write_config"):
        snapshot_prompts(batch_folder, spec_set_path=spec_set_path)
        plan_path = None
        if spec_set_path:
            plan = plan_spec_dealing(corpus_folder, batch_number, deals, retry_failed_specs, retry_failure_stages)
            plan_path = write_spec_dealing_plan_file(batch_folder, plan, batch_number, stamp)
        plan_in_folder = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path else None
        deals_in_config = len(plan_in_folder["forced_spec_ids"]) if plan_in_folder and plan_in_folder["forced_spec_ids"] \
            else deals
        write_batch_config(batch_folder, scenario_seed, deals_in_config, stamp, spec_set_path=spec_set_path,
                           spec_dealing_plan_path=plan_path)
        write_marker(batch_folder, "snapshot_prompts_and_write_config", 0)
    config_path = newest_file(batch_folder, "pipeline_config_for_this_batch_*.yaml")
    plan_file = newest_file(batch_folder, SPEC_DEALING_PLAN_FILE_PATTERN)
    batch_plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file else None
    batch_mode = batch_plan["mode"] if batch_plan else MODE_DEFAULT
    spec_attempt_numbers = spec_attempt_numbers_of_plan(batch_plan)
    deals = yaml.safe_load(config_path.read_text())["dad"]["dilemmas"]["count"]

    # pipeline step 1, resuming the partial run if one exists
    existing_run = find_pipeline_run_folder(batch_folder)
    resume_run_id = existing_run.name if existing_run and (existing_run / "step1").exists() else None
    run_step("pipeline_step_1", pipeline_command(config_path, batch_number, resume_run_id), PIPELINE_FOLDER,
             {"PIPELINE_OUTPUT_ROOT": str(batch_folder / "raw_pipeline_outputs")})
    step_one_folder = find_pipeline_run_folder(batch_folder) / "step1"

    run_step("selection", selection_command(step_one_folder, batch_folder))
    prompts_file = newest_file(batch_folder, "strictly_implicit_welfare_prompts_*_*.jsonl")
    if prompts_file is None:
        raise BatchFailure("selection produced no prompts file")
    prompt_records = read_jsonl(prompts_file)

    for judge_name, script, folder_name in (("answerability_judge", ANSWERABILITY_JUDGE_SCRIPT, "answerability_judge"),
                                            ("usefulness_judge", USEFULNESS_JUDGE_SCRIPT, "usefulness_judge")):
        for run_number in (1, 2):
            run_step(f"{judge_name}_run_{run_number}", judge_command(script, prompts_file, batch_folder / folder_name))

    prompt_ids = [record["prompt_gid"] for record in prompt_records]
    judges_with_missing_verdicts = []
    for judge_name, folder_name, pattern, step_prefix in (
            ("answerability judge", "answerability_judge", ANSWERABILITY_VERDICT_PATTERN, "answerability_judge"),
            ("usefulness judge", "usefulness_judge", USEFULNESS_VERDICT_PATTERN, "usefulness_judge")):
        missing_prompt_ids = prompt_ids_with_no_verdict_record_in_folder(batch_folder / folder_name, pattern, prompt_ids)
        if missing_prompt_ids:
            judges_with_missing_verdicts.append(f"{judge_name}: {len(missing_prompt_ids)} of {len(prompt_ids)} prompts")
            # forget that this judge's runs finished, so the resume runs it again (it skips prompts that already have verdicts)
            for marker_file in (batch_folder / "step_completion_markers").glob(f"{step_prefix}_run_*_completed_*.json"):
                marker_file.unlink()
    if judges_with_missing_verdicts:
        raise BatchFailure("judge calls failed for some prompts, so they have no verdict at all (" +
                           "; ".join(judges_with_missing_verdicts) + "). Nothing was written to the accepted file, "
                           "summary or ledger. Run the same command again to resume and judge them.")

    report("building accepted prompts")
    snapshot_folder = batch_folder / "generation_and_judge_prompts_used_for_this_batch"
    snapshotted_spec_set = snapshot_folder / spec_set_path.name if spec_set_path else None
    prompt_version_hash = generation_prompt_version_hash(snapshot_folder, profile_folder=snapshot_folder,
                                                      dealer_folder=snapshot_folder,
                                                      spec_set_path=snapshotted_spec_set)
    spec_set_fields = {"spec_set_file": spec_set_path.name if spec_set_path else None,
                       "spec_set_sha256": sha256_of_file(snapshotted_spec_set) if spec_set_path else None}
    judge_hashes = judge_prompt_hashes(snapshot_folder)
    answerability = ok_verdicts_in_folder(batch_folder / "answerability_judge", ANSWERABILITY_VERDICT_PATTERN)
    usefulness = ok_verdicts_in_folder(batch_folder / "usefulness_judge", USEFULNESS_VERDICT_PATTERN)
    generation_times = read_generation_times_state(batch_folder)
    generation_started_at = generation_times.get("batch_generation_started_at")
    generation_finished_at = generation_times.get("batch_generation_finished_at")
    accepted, counts = build_accepted_records(prompt_records, answerability, usefulness, batch_number, scenario_seed,
                                              prompt_version_hash, generation_started_at, generation_finished_at,
                                              spec_attempt_numbers)
    final_stamp = now().strftime(TIMESTAMP_FORMAT)
    accepted_path = batch_folder / f"accepted_prompts_batch_{batch_number:03d}_{final_stamp}.jsonl"
    accepted_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in accepted), encoding="utf-8")

    spec_outcomes_file, spec_outcome_summary = None, None
    if spec_set_path:
        spec_outcomes_path, spec_outcome_rows = write_batch_spec_outcomes_file(batch_folder, batch_number, final_stamp,
                                                                              spec_attempt_numbers)
        spec_outcomes_file, spec_outcome_summary = spec_outcomes_path.name, outcome_counts(spec_outcome_rows)
    mode_fields = {"batch_mode": batch_mode, "spec_outcomes_file": spec_outcomes_file,
                   "spec_outcome_counts": spec_outcome_summary}

    gate_passed_implicit = len(prompt_records)
    summary = {"batch_number": batch_number, "scenario_seed": scenario_seed, "deals_requested": deals,
               "implicit_gate_passed_count": gate_passed_implicit, "answerable_count": counts["answerable"],
               "useful_count": counts["useful"], "accepted_count": counts["accepted"],
               "unjudged_count": counts["unjudged"], "unjudged_prompt_ids": counts["unjudged_prompt_ids"],
               "generation_prompt_version_hash": prompt_version_hash, **spec_set_fields, **mode_fields, **judge_hashes,
               "batch_generation_started_at": generation_started_at,
               "batch_generation_finished_at": generation_finished_at,
               "started_at": started_at.isoformat(timespec="seconds"), "ended_at": now().isoformat(timespec="seconds"),
               "costs": cost_totals(batch_folder), "accepted_file": accepted_path.name}
    (batch_folder / f"batch_summary_{final_stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    ledger_entry = {"batch_number": batch_number,
                    "batch_folder": ledger_batch_folder_text(batch_folder, corpus_folder),
                    "scenario_seed": scenario_seed, "deals_requested": deals,
                    "implicit_gate_passed_count": gate_passed_implicit, "answerable_count": counts["answerable"],
                    "useful_count": counts["useful"], "accepted_count": counts["accepted"],
                    "unjudged_count": counts["unjudged"], "accepted_file": accepted_path.name,
                    "generation_prompt_version_hash": prompt_version_hash, **spec_set_fields, **mode_fields, **judge_hashes,
                    "batch_generation_started_at": generation_started_at,
                    "batch_generation_finished_at": generation_finished_at,
                    "started_at": summary["started_at"], "ended_at": summary["ended_at"], "costs": summary["costs"]}
    append_ledger_line(ledger_path, ledger_entry)
    if spec_set_path:
        spec_rows = read_jsonl(spec_set_path)
        table_path = write_spec_status_table(corpus_folder, spec_rows, batch_number, now().strftime(TIMESTAMP_FORMAT))
        report(f"spec status table written: {table_path.name}")
    report(f"ledger updated: {counts['accepted']} accepted of {gate_passed_implicit} selected "
           f"({counts['unjudged']} unjudged)")
    return ledger_entry


def print_plan(batch_number, seed, batch_folder, deals, corpus_folder, plan=None) -> None:
    print(f"Batch number: {batch_number}\nScenario seed: {seed}\nBatch folder: {batch_folder}\nDeals requested: {deals}")
    spec_set_path = spec_set_path_for_corpus(corpus_folder)
    print(f"Scenario seed base (from the corpus settings, or the default without them): "
          f"{read_corpus_settings(corpus_folder)['scenario_seed_base']}")
    print(f"Corpus folder: {corpus_folder}")
    print(f"Spec set: {spec_set_path if spec_set_path else 'none (no spec set file in the corpus settings)'}"
          + (f" (sha256 {sha256_of_file(spec_set_path)})" if spec_set_path else ""))
    print(f"Generation prompt version hash (includes the spec set file): "
          f"{generation_prompt_version_hash(spec_set_path=spec_set_path)}")
    if plan is not None:
        print(f"Mode: {plan['mode']}; specs dealt in earlier batches (never dealt again in default mode): "
              f"{len(plan['dealt_spec_ids']) - len(plan['reserved_spec_ids'])}; specs reserved for contributor shares "
              f"(never dealt here): {len(plan['reserved_spec_ids'])}; failed specs: {plan['failed_spec_count']}")
        print(f"Spec dealing plan (not part of the hash) would be written to: "
              f"{batch_folder / f'spec_dealing_plan_for_batch_{batch_number:03d}_<stamp>.json'}")
        if plan["forced_spec_ids"]:
            print(f"Specs that would be retried ({len(plan['forced_spec_ids'])}, fewest attempts first):")
            for spec in plan["retried_specs"]:
                print(f"  {spec['spec_id']}: attempts so far {spec['attempts_so_far']}, latest failure stage "
                      f"{spec['latest_failure_stage']}, {spec['taxa_category'][:40]} / {spec['taxa_subcategory']}")
    for name, value in judge_prompt_hashes().items():
        print(f"{name}: {value}")
    config_placeholder = batch_folder / "pipeline_config_for_this_batch_<stamp>.yaml"
    print("Commands that would run (nothing is created or run):")
    print(f"  cd {PIPELINE_FOLDER} && PIPELINE_OUTPUT_ROOT={batch_folder / 'raw_pipeline_outputs'} "
          f"{' '.join(pipeline_command(config_placeholder, batch_number))}")
    print(f"  {' '.join(selection_command(batch_folder / 'raw_pipeline_outputs/dad/runs/<run id>/step1', batch_folder))}")
    prompts_placeholder = batch_folder / "strictly_implicit_welfare_prompts_<count>_<stamp>.jsonl"
    for script, folder_name in ((ANSWERABILITY_JUDGE_SCRIPT, "answerability_judge"), (USEFULNESS_JUDGE_SCRIPT, "usefulness_judge")):
        for _ in range(2):
            print(f"  {' '.join(judge_command(script, prompts_placeholder, batch_folder / folder_name))}")
    print(f"After success, one line is appended to {corpus_folder / LEDGER_FILE_NAME}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-folder", type=Path, default=DEFAULT_CORPUS_FOLDER)
    parser.add_argument("--deals", type=int, default=64, help="number of scenario deals to request (default 64)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the commands; create nothing")
    parser.add_argument("--retry-failed-specs", action="store_true",
                        help="deal exactly the specs whose status is failed (fewest attempts first, at most --deals) "
                             "instead of never-dealt specs")
    parser.add_argument("--retry-failure-stages", default=None,
                        help="with --retry-failed-specs: only specs whose latest failure stage is in this comma-separated "
                             f"list (stages: {', '.join(FAILURE_STAGES)})")
    parser.add_argument("--resume-batch-folder", type=Path, default=None,
                        help="finish a batch folder that has no ledger entry yet")
    arguments = parser.parse_args(argv)
    # Windows extended-length paths (no effect elsewhere): the batch folder path alone can pass 259 characters.
    corpus_folder = extended_length_path(arguments.corpus_folder)
    retry_failure_stages = [stage.strip() for stage in arguments.retry_failure_stages.split(",")] \
        if arguments.retry_failure_stages else None
    unknown_stages = [stage for stage in retry_failure_stages or [] if stage not in FAILURE_STAGES]
    if unknown_stages:
        parser.error(f"unknown failure stages {unknown_stages}; choose from {list(FAILURE_STAGES)}")

    if arguments.resume_batch_folder is not None:
        batch_folder = extended_length_path(arguments.resume_batch_folder.resolve())  # resolve can drop the prefix; add it after
        match = BATCH_FOLDER_PATTERN.match(batch_folder.name)
        if not match or not batch_folder.is_dir():
            parser.error(f"{batch_folder} is not a batch folder named batch_<NNN>_seed_<seed>_<stamp>")
        batch_number, scenario_seed = int(match.group(1)), int(match.group(2))
        if batch_number in batch_numbers_in_ledger(corpus_folder / LEDGER_FILE_NAME):
            parser.error(f"batch {batch_number} is already in the ledger; nothing to resume")
        deals = yaml.safe_load(newest_file(batch_folder, "pipeline_config_for_this_batch_*.yaml").read_text())[
            "dad"]["dilemmas"]["count"] if newest_file(batch_folder, "pipeline_config_for_this_batch_*.yaml") else arguments.deals
    else:
        incomplete = incomplete_batch_folders(corpus_folder)
        if incomplete:
            print("Refusing to start a new batch: these batch folders have no ledger entry:", file=sys.stderr)
            for folder in incomplete:
                print(f"  {folder}", file=sys.stderr)
            print("Resume one with --resume-batch-folder <folder>.", file=sys.stderr)
            return 1
        batch_number = next_batch_number(corpus_folder)
        scenario_seed = scenario_seed_for_batch(batch_number, read_corpus_settings(corpus_folder)["scenario_seed_base"])
        deals = arguments.deals
        batch_folder = corpus_folder / "batches" / batch_folder_name(batch_number, scenario_seed,
                                                                      datetime.now().strftime(TIMESTAMP_FORMAT))

    plan = None
    try:
        if arguments.resume_batch_folder is None and spec_set_path_for_corpus(corpus_folder):
            # Plan and deal in memory first, so an exhausted category or an empty retry fails before any folder exists.
            plan = plan_spec_dealing(corpus_folder, batch_number, deals, arguments.retry_failed_specs,
                                     retry_failure_stages)
            preflight_deal(plan, corpus_folder, scenario_seed, deals)
        elif arguments.retry_failed_specs or retry_failure_stages:
            if arguments.resume_batch_folder is None:
                raise BatchFailure("--retry-failed-specs needs a corpus with a spec set")
    except BatchFailure as failure:
        if arguments.dry_run and "nothing to retry" in str(failure):
            print(f"Mode: {MODE_RETRY}\n{str(failure).capitalize()}")
            return 0
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1

    if arguments.dry_run:
        print_plan(batch_number, scenario_seed, batch_folder,
                   len(plan["forced_spec_ids"]) if plan and plan["forced_spec_ids"] else deals, corpus_folder, plan)
        return 0

    batch_folder.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()
    try:
        run_batch(corpus_folder, batch_folder, batch_number, scenario_seed, deals, started_at,
                  retry_failed_specs=arguments.retry_failed_specs, retry_failure_stages=retry_failure_stages)
    except BatchStoppedForUsageLimit as failure:
        print(remove_extended_length_prefixes_from_text(f"STOPPED FOR USAGE LIMIT: {failure}"), file=sys.stderr)
        return USAGE_LIMIT_EXIT_CODE
    except BatchFailure as failure:
        print(remove_extended_length_prefixes_from_text(f"FAILED: {failure}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
