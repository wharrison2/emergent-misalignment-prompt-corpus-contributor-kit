"""Smoke test: do the kit's real code paths work when every path is longer than Windows' 259 character limit?

    python scripts/emergent_misalignment/smoke_test_windows_long_path_handling_without_any_model_call.py

Makes no model call and needs no key. From a temporary folder whose paths are deliberately deeper than 260 characters it runs
the real code that a share run uses: the share's corpus folder and settings, the scenario seed record, snapshot_prompts into
a deep batch folder, the batch config and ledger line, a judge script's own dry run reading a prompts file from the deep
folder, the pipeline's run directory creation and a system prompt file at the deepest depth a share writes
(raw_pipeline_outputs/dad/runs/<run>/system_prompts/<64 hex>.txt), and the zip of the share. It prints PASS or FAIL for each
check and exits with a non-zero code if any check failed. On macOS and Linux the paths are long but legal, so it checks that
nothing is broken there; its value is on Windows, where it is run by the kit's GitHub Actions workflow with the
LongPathsEnabled setting turned off.

Everything is created in a temporary folder that is removed at the end (unless --keep-temporary-folder).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
KIT_OR_REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent.parent
PIPELINE_FOLDER = KIT_OR_REPOSITORY_ROOT / "strictly_implicit_difficult_advice_pipeline_copy"
RUN_MY_SHARES_CANDIDATES = [KIT_OR_REPOSITORY_ROOT / "run_my_shares.py", SCRIPT_DIRECTORY / "contributor_kit" / "run_my_shares.py"]
WINDOWS_PATH_LIMIT = 259
MINIMUM_LENGTH_OF_DEEPEST_FILE_PATH = 300
LENGTH_OF_TEMPORARY_BASE_FOLDER_PATH = 170
SHARE_NUMBER = 1
SCENARIO_SEED_BASE = 20280000

sys.path.insert(0, str(SCRIPT_DIRECTORY))
for candidate in RUN_MY_SHARES_CANDIDATES:
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent))
        break
import add_batch_to_emergent_misalignment_prompt_corpus as add_batch  # noqa: E402
import run_my_shares  # noqa: E402
from windows_extended_length_paths import extended_length_path, path_without_extended_length_prefix  # noqa: E402

# Run inside the pipeline folder as the batch script does (cwd = pipeline folder, PIPELINE_OUTPUT_ROOT in the environment),
# using the same two path steps as dad_pipeline/run.py: extend PIPELINE_OUTPUT_ROOT, then create the run directory.
PIPELINE_RUN_DIRECTORY_SNIPPET = '''
import json, os, sys
from pathlib import Path
from shared import api, utils
outputs_root = utils.extended_length_path(Path(os.environ["PIPELINE_OUTPUT_ROOT"]))
runs_root = outputs_root / "dad" / "runs"
run_directory = utils.create_run_dir(runs_root, label="prompt-corpus-batch-001", config={"model": "none"},
                                     snapshot_dirs={"prompts": Path(sys.argv[1])})
api.init_cost_log_only(run_directory / "cost_log.jsonl")
api._log_model_call("none", "none", "a system prompt written at the deepest depth", ["a user message"], "a reply")
print(json.dumps({"run_directory_name": run_directory.name}))
'''


class CheckRecorder:
    def __init__(self):
        self.failed_check_names = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        print(f"{'PASS' if passed else 'FAIL'}: {name}" + (f" ({detail})" if detail and not passed else ""), flush=True)
        if not passed:
            self.failed_check_names.append(name)


def make_deep_base_folder(temporary_folder: Path) -> Path:
    """A folder inside the temporary folder whose own path is LENGTH_OF_TEMPORARY_BASE_FOLDER_PATH characters long."""
    prefix_length = len(str(temporary_folder)) + 1
    padding_length = max(8, LENGTH_OF_TEMPORARY_BASE_FOLDER_PATH - prefix_length)
    base_folder = Path(temporary_folder) / ("deliberately_deep_folder_" + "d" * padding_length)[:padding_length]
    extended_length_path(base_folder).mkdir(parents=True)
    return base_folder


def write_fake_share_file(share_folder: Path) -> dict:
    share_folder.mkdir(parents=True, exist_ok=True)
    share_file_name = f"fake_share_{SHARE_NUMBER:03d}_spec_set.jsonl"
    content = json.dumps({"spec_id": "S-001"}) + "\n"
    (share_folder / share_file_name).write_text(content, encoding="utf-8")
    return {"share_number": SHARE_NUMBER, "share_file": share_file_name, "scenario_seed_base": SCENARIO_SEED_BASE,
            "share_file_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


def run_checks(base_folder: Path, recorder: CheckRecorder) -> None:
    output_folder = extended_length_path(base_folder / "my_share_corpora")
    corpus_folder = run_my_shares.corpus_folder_for_share(SHARE_NUMBER, output_folder, "")
    share = write_fake_share_file(extended_length_path(base_folder / "fake_share_folder"))
    share_folder = extended_length_path(base_folder / "fake_share_folder")
    run_my_shares.KIT_ROOT = base_folder  # so that relative_to_kit has a kit folder to be relative to
    run_my_shares.SCENARIO_SEEDS_RECORD_PATH = extended_length_path(base_folder / "scenario_seeds_already_started.json")

    # 1. corpus folder and settings (the corpus folder is the first deep folder)
    run_my_shares.prepare_share_corpus_folder(share, corpus_folder, share_folder)
    spec_set_path = add_batch.spec_set_path_for_corpus(corpus_folder)
    recorder.check("share corpus folder holds the settings file and a copy of the spec set",
                   (corpus_folder / run_my_shares.CORPUS_SETTINGS_FILE_NAME).exists() and spec_set_path.exists())

    # 2. the batch folder and snapshot_prompts, the step that failed with WinError 3
    batch_folder = corpus_folder / "batches" / add_batch.batch_folder_name(1, SCENARIO_SEED_BASE + 1, "2026-11-09_12-00")
    batch_folder.mkdir(parents=True)
    batch_folder_length = len(str(path_without_extended_length_prefix(batch_folder)))
    recorder.check(f"the batch folder path is longer than {WINDOWS_PATH_LIMIT} characters (it is {batch_folder_length})",
                   batch_folder_length > WINDOWS_PATH_LIMIT)
    add_batch.snapshot_prompts(batch_folder, spec_set_path=spec_set_path)
    snapshot_folder = batch_folder / "generation_and_judge_prompts_used_for_this_batch"
    expected_snapshot_names = (add_batch.GENERATION_PROMPT_FILE_NAMES + [add_batch.GENERATION_PROFILE_FILE_NAME,
                               add_batch.GENERATION_DEALER_FILE_NAME, add_batch.ANSWERABILITY_JUDGE_TEMPLATE_NAME,
                               add_batch.USEFULNESS_JUDGE_TEMPLATE_NAME, spec_set_path.name])
    missing_snapshot_names = [name for name in expected_snapshot_names if not (snapshot_folder / name).exists()]
    recorder.check("snapshot_prompts copied every prompt, profile, dealer, judge template and spec set into the deep folder",
                   not missing_snapshot_names, f"missing {missing_snapshot_names}")

    # 3. the batch config (an output file: it must not hold the extended-length prefix) and the ledger line
    plan_path = add_batch.write_spec_dealing_plan_file(batch_folder, {"mode": add_batch.MODE_DEFAULT, "dealt_spec_ids": []}, 1,
                                                       "2026-11-09_12-00")
    config_path = add_batch.write_batch_config(batch_folder, SCENARIO_SEED_BASE + 1, 1, "2026-11-09_12-00",
                                               spec_set_path=spec_set_path, spec_dealing_plan_path=plan_path)
    config_text = config_path.read_text(encoding="utf-8")
    recorder.check("the batch config is written and names the spec set and plan without an extended-length prefix",
                   "?" not in config_text and spec_set_path.name in config_text and plan_path.name in config_text)
    ledger_text = add_batch.ledger_batch_folder_text(batch_folder, corpus_folder)
    recorder.check("the ledger's batch folder is a relative path with '/' and no prefix",
                   ledger_text == f"batches/{batch_folder.name}", ledger_text)
    add_batch.append_ledger_line(corpus_folder / add_batch.LEDGER_FILE_NAME, {"batch_number": 1, "batch_folder": ledger_text})

    # 4. the pipeline: the same environment variable, working folder and run directory code as the batch script uses
    environment = dict(os.environ, PIPELINE_OUTPUT_ROOT=str(batch_folder / "raw_pipeline_outputs"))
    completed = subprocess.run([sys.executable, "-c", PIPELINE_RUN_DIRECTORY_SNIPPET, str(snapshot_folder)],
                               cwd=PIPELINE_FOLDER, env=environment, capture_output=True, text=True)
    recorder.check("the pipeline creates its run directory and writes model call files under the deep PIPELINE_OUTPUT_ROOT",
                   completed.returncode == 0, (completed.stderr or completed.stdout)[-600:])
    system_prompt_files = list((batch_folder / "raw_pipeline_outputs").rglob("system_prompts/*.txt"))
    recorder.check("a system prompt file exists at the deepest depth a share writes", len(system_prompt_files) == 1)
    deepest_length = max((len(str(path_without_extended_length_prefix(path))) for path in system_prompt_files), default=0)
    recorder.check(f"that file's path is longer than {MINIMUM_LENGTH_OF_DEEPEST_FILE_PATH} characters (it is {deepest_length})",
                   deepest_length > MINIMUM_LENGTH_OF_DEEPEST_FILE_PATH)
    pipeline_run_folder = add_batch.find_pipeline_run_folder(batch_folder)
    recorder.check("the batch script finds the pipeline's run folder again (the resume path)",
                   pipeline_run_folder is not None and (pipeline_run_folder / "run_manifest.json").exists())

    # 5. a judge script's own dry run, which reads a prompts file from the deep folder (no model call)
    prompts_file = batch_folder / "strictly_implicit_welfare_prompts_1_2026-11-09_12-00.jsonl"
    prompts_file.write_text(json.dumps({"prompt_gid": "g1", "user_message": "a short user message"}) + "\n", encoding="utf-8")
    judge_run = subprocess.run([sys.executable, str(add_batch.ANSWERABILITY_JUDGE_SCRIPT), "--prompts-file", str(prompts_file),
                                "--output-folder", str(batch_folder / "answerability_judge"), "--dry-run"],
                               cwd=PIPELINE_FOLDER, capture_output=True, text=True)
    recorder.check("a judge script reads the prompts file from the deep folder in its dry run",
                   judge_run.returncode == 0 and "DRY RUN: 1 prompts" in judge_run.stdout,
                   (judge_run.stderr or judge_run.stdout)[-600:])

    # 6. the seed record: a prefixed and an unprefixed form of the same folder must be the same folder
    run_my_shares.append_scenario_seed_record(share, corpus_folder)
    record = run_my_shares.find_scenario_seed_record(SCENARIO_SEED_BASE)
    recorder.check("the seed record holds a relative folder without a prefix", record is not None and "?" not in record["corpus_folder"],
                   str(record))
    plain_corpus_folder = path_without_extended_length_prefix(corpus_folder)
    recorder.check("the seed record matches the share folder in its prefixed and its plain form",
                   record is not None
                   and run_my_shares.same_relative_folder(record["corpus_folder"], run_my_shares.relative_to_kit(corpus_folder))
                   and run_my_shares.same_relative_folder(record["corpus_folder"], run_my_shares.relative_to_kit(plain_corpus_folder)))

    # 7. the zip: entry names are relative to the share, with no prefix, and every file (including the deepest) is in it
    zip_path = run_my_shares.write_zip_of_share_corpora([SHARE_NUMBER], output_folder, base_folder / "zips_to_send_back")
    with zipfile.ZipFile(zip_path) as archive:
        entry_names = archive.namelist()
    files_on_disk = [path for path in corpus_folder.rglob("*") if path.is_file() and path.name != run_my_shares.LOCK_FILE_NAME]
    recorder.check("the zip holds every file of the share", len(entry_names) == len(files_on_disk) > 0,
                   f"{len(entry_names)} entries, {len(files_on_disk)} files")
    recorder.check("every zip entry name starts with contributor_corpus_share_NNN/ and has no prefix or backslash",
                   all(name.startswith(f"contributor_corpus_share_{SHARE_NUMBER:03d}/") and "?" not in name and "\\" not in name
                       for name in entry_names))
    recorder.check("the deepest file is in the zip under its relative name",
                   any(name.endswith(f"system_prompts/{system_prompt_files[0].name}") for name in entry_names)
                   if system_prompt_files else False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-temporary-folder", action="store_true", help="do not delete the deep temporary folder at the end")
    arguments = parser.parse_args(argv)
    recorder = CheckRecorder()
    temporary_folder = Path(tempfile.mkdtemp(prefix="long_path_smoke_test_")).resolve()
    print(f"Python {sys.version.split()[0]} on {sys.platform}; temporary folder {temporary_folder}", flush=True)
    try:
        run_checks(make_deep_base_folder(temporary_folder), recorder)
    except Exception as error:  # a crash is a failure, with its message, not a traceback that hides the other checks
        import traceback
        traceback.print_exc()
        recorder.check(f"no unexpected error ({type(error).__name__}: {error})", False)
    finally:
        if not arguments.keep_temporary_folder:
            shutil.rmtree(extended_length_path(temporary_folder), ignore_errors=True)
    if recorder.failed_check_names:
        print(f"\nFAIL: {len(recorder.failed_check_names)} check(s) failed: " + "; ".join(recorder.failed_check_names), flush=True)
        return 1
    print("\nPASS: every check passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
