"""Run your assigned shares of the emergent misalignment prompt corpus, one batch per share, and zip the results.

    python run_my_shares.py --shares 7,12

For each share this creates (or reuses) my_share_corpora/contributor_corpus_share_NNN/ with a settings file naming the
share's spec set file and scenario seed base, then runs ONE batch there that deals the share's safe number of specs
(add_batch_to_emergent_misalignment_prompt_corpus.py does the work: scenario generation, drafting and gating through
Claude Code, then two judges). Run it again with the same command after any stop: a finished share is skipped and an
unfinished batch is resumed from its checkpoints. When every share you asked for is complete, it writes one zip in
zips_to_send_back/ named with the share numbers and the date and time. Send that zip back.

A share is only ever run once per scenario seed: the kit keeps a record of every seed it has started
(scenario_seeds_already_started_in_this_kit.json), and a run that would repeat one is refused, even if the share's folder was
moved or deleted, because it would only duplicate prompts. If every share you asked for was already run, nothing runs, no new
zip is written and the exit code is 4; run your next share, or use --second-pass once all your assigned shares have been run once.

All model calls go through the Claude Code command line tool, billed to your Claude subscription. No API key is read or
needed. If your usage limit is reached the run stops cleanly (exit code 3): wait for the limit to reset and rerun.

Second pass: `--second-pass` deals the same specs again with a different scenario seed (the share's seed base plus 500) so each
spec gets a second prompt with different non-spec dealt variables. It writes to its own folder (second_pass_share_corpora/ by
default, in folders named second_pass_contributor_corpus_share_NNN), never touching the first pass, and finds the share's safe deal count for the new seed by simulating the deal offline.
`--scenario-seed-base N` does the same with an explicit seed base, for exactly one share, and needs its own --output-folder.

--dry-run prints what would run for each share and creates nothing in your output folder. --status prints the state of
each share. Neither makes a model call.
"""

import argparse
import hashlib
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent
BATCH_SCRIPT = KIT_ROOT / "scripts" / "emergent_misalignment" / "add_batch_to_emergent_misalignment_prompt_corpus.py"
SHARE_FOLDER = KIT_ROOT / "contributor_shares"
MANIFEST_PATTERN = "contributor_share_manifest_*.json"
DEFAULT_OUTPUT_FOLDER = KIT_ROOT / "my_share_corpora"
DEFAULT_SECOND_PASS_OUTPUT_FOLDER = KIT_ROOT / "second_pass_share_corpora"
SECOND_PASS_SEED_BASE_OFFSET = 500
BATCH_NUMBER_OF_EVERY_SHARE = 1  # one batch per share, so its scenario seed is the seed base plus 1
MAIN_CORPUS_SEED_RANGE = range(20280001, 20281000)  # 20280001-20280999, the main corpus's batch seeds
DEAL_COUNT_RECORD_FILE_NAME = "deal_count_found_by_offline_simulation.json"
ZIP_FOLDER = KIT_ROOT / "zips_to_send_back"
CORPUS_SETTINGS_FILE_NAME = "corpus_settings_scenario_seed_base_and_spec_set_file.json"
LEDGER_FILE_NAME = "corpus_batch_ledger_started_2026-11-06.jsonl"
LOCK_FILE_NAME = "run_my_shares_lock_while_a_run_is_in_progress.json"
BATCH_FOLDER_PREFIX = "batch_"
USAGE_LIMIT_EXIT_CODE = 3
ALREADY_RUN_EXIT_CODE = 4
SCENARIO_SEEDS_RECORD_PATH = KIT_ROOT / "scenario_seeds_already_started_in_this_kit.json"
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"
MINIMUM_PYTHON = (3, 10)
SUBSCRIPTION_AUTH_METHOD = "claude.ai"

SCRIPTS_FOLDER = KIT_ROOT / "scripts" / "emergent_misalignment"

STATE_COMPLETE = "complete"
STATE_UNFINISHED_BATCH = "unfinished_batch"
STATE_NOT_STARTED = "not_started"


def relative_to_kit(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(KIT_ROOT))
    except ValueError:
        return str(path)


def parse_share_numbers(text: str) -> list[int]:
    try:
        numbers = [int(part) for part in text.replace(" ", "").split(",") if part]
    except ValueError:
        raise SystemExit(f"--shares must be comma-separated share numbers such as 7,12 (got {text!r})")
    if not numbers or len(set(numbers)) != len(numbers):
        raise SystemExit("--shares needs at least one share number, each listed once")
    return numbers


def load_newest_manifest(share_folder: Path = SHARE_FOLDER) -> dict:
    manifests = sorted(Path(share_folder).glob(MANIFEST_PATTERN))
    if not manifests:
        raise SystemExit(f"No contributor share manifest found in {relative_to_kit(share_folder)}. Is the kit complete?")
    return json.loads(manifests[-1].read_text(encoding="utf-8"))


def find_share(manifest: dict, share_number: int) -> dict:
    for share in manifest["shares"]:
        if share["share_number"] == share_number:
            return share
    available = ", ".join(str(share["share_number"]) for share in manifest["shares"])
    raise SystemExit(f"There is no share {share_number}. Shares in this kit: {available}")


def verified_share_file(share: dict, share_folder: Path = SHARE_FOLDER) -> Path:
    path = Path(share_folder) / share["share_file"]
    if not path.exists():
        raise SystemExit(f"Share file {relative_to_kit(path)} is missing. Re-clone the repository.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != share["share_file_sha256"]:
        raise SystemExit(f"Share file {relative_to_kit(path)} does not match the checksum in the manifest. "
                         f"Do not edit share files; re-clone the repository.")
    return path


def corpus_folder_for_share(share_number: int, output_folder: Path, label: str = "") -> Path:
    """The share's corpus folder: the label ('' for a first pass, 'second_pass_' for any other pass) then
    contributor_corpus_share_NNN, so a second pass is named as one wherever the folder is copied or zipped.
    A second pass run by an earlier kit version used the unprefixed name; when only that old-named folder exists it is
    still the folder this pass started in, so it is returned instead (never for a first pass, label '')."""
    prefixed_folder = Path(output_folder) / f"{label}contributor_corpus_share_{share_number:03d}"
    legacy_folder = Path(output_folder) / f"contributor_corpus_share_{share_number:03d}"
    if label and not prefixed_folder.exists() and legacy_folder.exists():
        return legacy_folder
    return prefixed_folder


def settings_for_share(share: dict) -> dict:
    return {"scenario_seed_base": share["scenario_seed_base"], "spec_set_file": f"fixed_spec_set/{share['share_file']}"}


def prepare_share_corpus_folder(share: dict, corpus_folder: Path, share_folder: Path = SHARE_FOLDER) -> None:
    """Create the corpus folder with the share's spec set and settings; refuse a folder whose settings differ."""
    share_file = verified_share_file(share, share_folder)
    settings_path = Path(corpus_folder) / CORPUS_SETTINGS_FILE_NAME
    expected = settings_for_share(share)
    if settings_path.exists():
        if json.loads(settings_path.read_text(encoding="utf-8")) != expected:
            raise SystemExit(f"{relative_to_kit(settings_path)} does not match share {share['share_number']}. "
                             f"Do not move, rename or delete it: ask the person who sent you the kit.")
        return
    (Path(corpus_folder) / "fixed_spec_set").mkdir(parents=True, exist_ok=True)
    shutil.copy2(share_file, Path(corpus_folder) / expected["spec_set_file"])
    settings_path.write_text(json.dumps(expected, indent=2), encoding="utf-8")


def unfinished_batch_folders(corpus_folder: Path) -> list[Path]:
    batches_folder = Path(corpus_folder) / "batches"
    if not batches_folder.exists():
        return []
    finished = set()
    ledger_path = Path(corpus_folder) / LEDGER_FILE_NAME
    if ledger_path.exists():
        finished = {json.loads(line)["batch_folder"] for line in ledger_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()}
    return [folder for folder in sorted(batches_folder.iterdir())
            if folder.is_dir() and folder.name.startswith(BATCH_FOLDER_PREFIX)
            and f"batches/{folder.name}" not in finished]


def state_of_share_corpus(corpus_folder: Path) -> tuple[str, Path | None]:
    ledger_path = Path(corpus_folder) / LEDGER_FILE_NAME
    if ledger_path.exists() and ledger_path.read_text(encoding="utf-8").strip():
        return STATE_COMPLETE, None
    unfinished = unfinished_batch_folders(corpus_folder)
    if len(unfinished) > 1:
        raise SystemExit(f"{relative_to_kit(corpus_folder)} has {len(unfinished)} unfinished batch folders; there should be "
                         f"one. Ask the person who sent you the kit.")
    return (STATE_UNFINISHED_BATCH, unfinished[0]) if unfinished else (STATE_NOT_STARTED, None)


# ---------- the record of seeds already started, and what to tell the contributor when a share was already run ----------

def guidance_when_share_already_run(share: dict) -> str:
    number = share["share_number"]
    if share.get("is_second_pass"):
        return (f"Share {number:03d} has had its second pass run already, so both passes of this share are done and there is "
                f"nothing more to run for it.")
    return (f"Share {number:03d} has already been run once. Run the next of your assigned shares that is not complete "
            f"(`python run_my_shares.py --shares <all your shares> --status` shows which). Only when every one of your assigned "
            f"shares has been run once, you can run a second pass with `python run_my_shares.py --shares {number} --second-pass`: "
            f"it deals the same specs again with a different seed, so each spec gets a second prompt with a different kind of "
            f"person, message form and so on. Until then, an unrun share is more useful, because it adds prompts for specs "
            f"that have none yet.")


def load_scenario_seed_records() -> list[dict]:
    if not SCENARIO_SEEDS_RECORD_PATH.exists():
        return []
    return json.loads(SCENARIO_SEEDS_RECORD_PATH.read_text(encoding="utf-8"))


def find_scenario_seed_record(scenario_seed_base: int) -> dict | None:
    for record in load_scenario_seed_records():
        if record["scenario_seed_base"] == scenario_seed_base:
            return record
    return None


def append_scenario_seed_record(share: dict, corpus_folder: Path) -> None:
    records = load_scenario_seed_records()
    records.append({"share_number": share["share_number"], "scenario_seed_base": share["scenario_seed_base"],
                    "batch_scenario_seed": share["scenario_seed_base"] + BATCH_NUMBER_OF_EVERY_SHARE,
                    "corpus_folder": relative_to_kit(corpus_folder),
                    "pass_kind": "second pass" if share.get("is_second_pass") else "first pass",
                    "started_at": datetime.now().isoformat(timespec="seconds")})
    SCENARIO_SEEDS_RECORD_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")


def refuse_scenario_seed_already_started(share: dict, record: dict) -> None:
    raise SystemExit(f"Nothing was run. Share {share['share_number']:03d} was already started with scenario seed base "
                     f"{share['scenario_seed_base']} on this computer ({record['pass_kind']}, in {record['corpus_folder']}, "
                     f"at {record['started_at']}). Running it again would only duplicate those prompts. "
                     f"{guidance_when_share_already_run(share)}")


# ---------- seeds and the offline deal simulation (no model call) ----------

def second_pass_seed_base(share: dict) -> int:
    return share["scenario_seed_base"] + SECOND_PASS_SEED_BASE_OFFSET


def refuse_seed_base_that_could_repeat_a_seed(seed_base: int, manifest: dict, is_standard_second_pass: bool = False) -> None:
    """Exit with a message when the seed base, or the batch seed it gives, could repeat any seed used by the main corpus,
    a first pass, or a standard second pass."""
    batch_seed = seed_base + BATCH_NUMBER_OF_EVERY_SHARE
    first_pass_bases = {share["scenario_seed_base"] for share in manifest["shares"]}
    first_pass_batch_seeds = {share["first_batch_scenario_seed"] for share in manifest["shares"]}
    standard_second_pass_bases = {base + SECOND_PASS_SEED_BASE_OFFSET for base in first_pass_bases}
    standard_second_pass_batch_seeds = {base + BATCH_NUMBER_OF_EVERY_SHARE for base in standard_second_pass_bases}
    reasons = []
    if seed_base in first_pass_bases:
        reasons.append("it is a share's first-pass seed base")
    if batch_seed in first_pass_batch_seeds:
        reasons.append("its batch seed is a share's first-pass batch seed")
    if not is_standard_second_pass and (seed_base in standard_second_pass_bases or batch_seed in standard_second_pass_batch_seeds):
        reasons.append("it is the seed of a standard --second-pass")
    if seed_base in MAIN_CORPUS_SEED_RANGE or batch_seed in MAIN_CORPUS_SEED_RANGE:
        reasons.append(f"it is in the main corpus's seed range {MAIN_CORPUS_SEED_RANGE[0]}-{MAIN_CORPUS_SEED_RANGE[-1]}")
    if reasons:
        raise SystemExit(f"Scenario seed base {seed_base} is refused: {'; '.join(reasons)}. Pick a seed base whose seed "
                         f"({seed_base} + {BATCH_NUMBER_OF_EVERY_SHARE}) nothing else has used.")


def load_scenario_dealer_module():
    if str(SCRIPTS_FOLDER) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_FOLDER))
    from load_pipeline_scenario_dealer import load_scenario_dealer
    return load_scenario_dealer()


def simulate_deal_of_specs(spec_set_path: Path, deal_count: int, scenario_seed: int) -> list[str]:
    """The spec ids the real scenario dealer deals for this spec set, count and seed (nothing dealt before). Raises the
    dealer's SpecPoolExhausted when a dealt category has no spec left."""
    compose_scenarios = load_scenario_dealer_module()
    scenarios = compose_scenarios.deal_scenarios(deal_count, random.Random(scenario_seed),
                                                 compose_scenarios.DEFAULT_VARIABLES, Path(spec_set_path), None)
    return [scenario["spec_id"] for scenario in scenarios]


def find_largest_safe_deal_count(spec_set_path: Path, share_size: int, scenario_seed: int) -> tuple[int, str | None]:
    """(largest count up to share_size that deals without exhausting a category, the category that exhausted at
    share_size or None when the whole share can be dealt). Searched downwards because the deal changes with the count."""
    compose_scenarios = load_scenario_dealer_module()
    exhausted_category_at_full_size = None
    for deal_count in range(share_size, 0, -1):
        try:
            simulate_deal_of_specs(spec_set_path, deal_count, scenario_seed)
            return deal_count, exhausted_category_at_full_size
        except compose_scenarios.SpecPoolExhausted as error:
            if deal_count == share_size:
                exhausted_category_at_full_size = error.category
    raise SystemExit(f"No deal count from 1 to {share_size} is safe for scenario seed {scenario_seed}.")


def share_as_dealt_with_seed_base(share: dict, seed_base: int, share_folder: Path | None = None) -> dict:
    """The share with the given scenario seed base. At its own manifest seed base the manifest's safe deal count stands;
    at any other, the safe deal count is found by simulating the deal offline at the new seed."""
    if seed_base == share["scenario_seed_base"]:
        return share
    share_file = verified_share_file(share, share_folder or SHARE_FOLDER)
    batch_seed = seed_base + BATCH_NUMBER_OF_EVERY_SHARE
    safe_count, exhausted_category = find_largest_safe_deal_count(share_file, share["spec_count"], batch_seed)
    dealt_ids = simulate_deal_of_specs(share_file, safe_count, batch_seed)
    if not set(dealt_ids) <= set(share["spec_ids"]):
        raise SystemExit(f"The simulated deal for share {share['share_number']} would deal specs outside the share.")
    return dict(share, scenario_seed_base=seed_base, first_batch_scenario_seed=batch_seed, safe_deal_count=safe_count,
                category_exhausted_when_dealing_the_whole_share=exhausted_category,
                specs_unused_at_safe_deal_count=share["spec_count"] - safe_count, is_second_pass=True)


def record_deal_count(corpus_folder: Path, share: dict) -> None:
    """Keep next to the corpus the seed and deal count this run used (informational; the settings file is the check)."""
    Path(corpus_folder).mkdir(parents=True, exist_ok=True)
    (Path(corpus_folder) / DEAL_COUNT_RECORD_FILE_NAME).write_text(json.dumps({
        "share_number": share["share_number"], "scenario_seed_base": share["scenario_seed_base"],
        "batch_scenario_seed": share["scenario_seed_base"] + BATCH_NUMBER_OF_EVERY_SHARE,
        "deal_count_used": share["safe_deal_count"], "specs_in_share": share["spec_count"],
        "category_exhausted_when_dealing_the_whole_share": share.get("category_exhausted_when_dealing_the_whole_share"),
        "recorded_at": datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")


def choose_seed_bases_and_output_folder(arguments, shares: list[dict], manifest: dict) -> tuple[list[dict], Path, str]:
    """Apply --second-pass / --scenario-seed-base with all the refusals, before anything is created. Returns the shares as
    they will be dealt, the output folder, and the label that prefixes the zip and corpus folder names ('' for a first pass)."""
    if arguments.second_pass and arguments.scenario_seed_base is not None:
        raise SystemExit("Use either --second-pass or --scenario-seed-base, not both.")
    if arguments.scenario_seed_base is None and not arguments.second_pass:
        return shares, (arguments.output_folder or DEFAULT_OUTPUT_FOLDER).resolve(), ""
    if arguments.scenario_seed_base is not None:
        if len(shares) != 1:
            raise SystemExit("--scenario-seed-base is an absolute seed base, so it needs exactly one share in --shares. "
                             "For several shares use --second-pass.")
        if arguments.output_folder is None:
            raise SystemExit("--scenario-seed-base needs its own --output-folder (for example --output-folder "
                             "my_second_pass_corpora), so a pass with another seed never lands in my_share_corpora/.")
        seed_bases = [arguments.scenario_seed_base]
    else:
        seed_bases = [second_pass_seed_base(share) for share in shares]
    output_folder = (arguments.output_folder or DEFAULT_SECOND_PASS_OUTPUT_FOLDER).resolve()
    if output_folder == DEFAULT_OUTPUT_FOLDER.resolve() and any(base != share["scenario_seed_base"]
                                                                for base, share in zip(seed_bases, shares)):
        raise SystemExit(f"A second pass must write to its own output folder, not {DEFAULT_OUTPUT_FOLDER.name}/ (that "
                         f"holds the first pass). Leave --output-folder out with --second-pass, or name a new folder.")
    for seed_base in seed_bases:
        refuse_seed_base_that_could_repeat_a_seed(seed_base, manifest, is_standard_second_pass=arguments.second_pass)
    return ([share_as_dealt_with_seed_base(share, base) for share, base in zip(shares, seed_bases)], output_folder,
            "second_pass_")


def batch_command(corpus_folder: Path, share: dict, unfinished_batch_folder: Path | None) -> list[str]:
    command = [sys.executable, str(BATCH_SCRIPT), "--corpus-folder", str(corpus_folder),
               "--deals", str(share["safe_deal_count"])]
    if unfinished_batch_folder is not None:
        command += ["--resume-batch-folder", str(unfinished_batch_folder)]
    return command


# ---------- checks that make no model call ----------

def check_python_and_packages() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        raise SystemExit(f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is needed; this is {sys.version.split()[0]}.")
    missing = []
    for module_name in ("yaml", "dotenv", "tenacity", "anthropic", "claude_agent_sdk"):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        raise SystemExit(f"Missing Python packages ({', '.join(missing)}). Activate the virtual environment and run: "
                         f"pip install -r requirements.txt")


def find_claude_command() -> str | None:
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    try:
        import claude_agent_sdk
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        return str(bundled) if bundled.exists() else None
    except ImportError:
        return None


def check_claude_login(accept_other_login_method: bool, run=subprocess.run) -> str:
    """Ask the Claude Code command line tool for its login status (`claude auth status`, which makes no model call).
    Returns a one-line description; raises SystemExit when the tool is missing, logged out, or logged in some way other
    than a Claude subscription (unless accepted)."""
    command = find_claude_command()
    if command is None:
        raise SystemExit("The Claude Code command line tool was not found. Install it (https://docs.claude.com/en/docs/"
                         "claude-code), then run: claude auth login")
    try:
        result = run([command, "auth", "status", "--json"], capture_output=True, text=True, timeout=60)
        status = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "could not check the Claude Code login (no zero-cost check answered); continuing"
    if not status.get("loggedIn"):
        raise SystemExit("Claude Code is not logged in. Run: claude auth login   (sign in with your Claude subscription), "
                         "then rerun this command.")
    method = status.get("authMethod")
    if method != SUBSCRIPTION_AUTH_METHOD and not accept_other_login_method:
        raise SystemExit(f"Claude Code is logged in with {method!r}, not a Claude subscription ({SUBSCRIPTION_AUTH_METHOD!r}); "
                         f"that could bill an API account. Log in with your subscription (claude auth login), or add "
                         f"--accept-other-login-method if you are sure.")
    return f"Claude Code is logged in (method: {method})"


# ---------- locking and zipping ----------

def process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def take_lock(corpus_folder: Path) -> Path:
    lock_path = Path(corpus_folder) / LOCK_FILE_NAME
    if lock_path.exists():
        try:
            holder = json.loads(lock_path.read_text(encoding="utf-8")).get("process_id")
        except ValueError:
            holder = None
        if holder and process_is_alive(holder):
            raise SystemExit(f"Another run (process {holder}) is already working on {relative_to_kit(corpus_folder)}. "
                             f"Wait for it, or stop it first.")
    lock_path.write_text(json.dumps({"process_id": os.getpid(), "taken_at": datetime.now().isoformat(timespec="seconds")}),
                         encoding="utf-8")
    return lock_path


def write_zip_of_share_corpora(share_numbers: list[int], output_folder: Path, zip_folder: Path = ZIP_FOLDER,
                               now=datetime.now, label: str = "") -> Path:
    zip_folder.mkdir(parents=True, exist_ok=True)
    share_text = "_".join(f"{number:03d}" for number in sorted(share_numbers))
    zip_path = zip_folder / (f"emergent_misalignment_contributor_corpus_{label}shares_{share_text}_"
                             f"{now().strftime(TIMESTAMP_FORMAT)}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for number in sorted(share_numbers):
            corpus_folder = corpus_folder_for_share(number, output_folder, label)
            for path in sorted(corpus_folder.rglob("*")):
                if path.is_file() and path.name != LOCK_FILE_NAME:
                    # A second pass's folder is named second_pass_contributor_corpus_share_NNN; one run by an earlier kit
                    # version has the unprefixed name on disk, so the name inside the zip is built from the label here,
                    # once, so an unzipped second pass is never mistaken for a first pass.
                    archive.write(path, Path(f"{label}contributor_corpus_share_{number:03d}") / path.relative_to(corpus_folder))
    return zip_path


# ---------- main ----------

def run_share(share: dict, output_folder: Path, label: str = "") -> int:
    """Run (or resume) the share's one batch. Returns 0 when the share is complete, else the batch script's exit code."""
    number = share["share_number"]
    corpus_folder = corpus_folder_for_share(number, output_folder, label)
    # A seed already started is only allowed to continue in the very folder it started in (to finish or retry it); a folder
    # that was moved away or deleted must never let the same seed run again from scratch.
    seed_record = find_scenario_seed_record(share["scenario_seed_base"])
    continues_recorded_folder = (seed_record is not None and corpus_folder.exists()
                                 and seed_record["corpus_folder"] == relative_to_kit(corpus_folder))
    if seed_record is not None and not continues_recorded_folder:
        refuse_scenario_seed_already_started(share, seed_record)
    prepare_share_corpus_folder(share, corpus_folder)
    state, unfinished = state_of_share_corpus(corpus_folder)
    if state == STATE_COMPLETE:
        print(f"Share {number:03d}: already complete, skipping.", flush=True)
        return 0
    if state == STATE_NOT_STARTED and seed_record is None:
        append_scenario_seed_record(share, corpus_folder)
    record_deal_count(corpus_folder, share)
    print(f"Share {number:03d}: scenario seed base {share['scenario_seed_base']}, deal count {share['safe_deal_count']} of "
          f"{share['spec_count']} specs.", flush=True)
    lock_path = take_lock(corpus_folder)
    try:
        action = f"resuming {relative_to_kit(unfinished)}" if unfinished else f"starting a batch of {share['safe_deal_count']} specs"
        print(f"[{datetime.now():%H:%M:%S}] Share {number:03d}: {action}. Progress is reported about every 3 minutes.", flush=True)
        return subprocess.call(batch_command(corpus_folder, share, unfinished), cwd=KIT_ROOT)
    finally:
        lock_path.unlink(missing_ok=True)


def print_dry_run(share: dict, output_folder: Path, label: str = "") -> None:
    """Show the batch script's own dry run for the share, in a throwaway corpus folder, so nothing is created in yours."""
    corpus_folder = corpus_folder_for_share(share["share_number"], output_folder, label)
    state = state_of_share_corpus(corpus_folder) if corpus_folder.exists() else (STATE_NOT_STARTED, None)
    pass_note = ", second pass: deal count found by offline simulation at this seed" if share.get("is_second_pass") else ""
    print(f"\n=== Share {share['share_number']:03d} ({share['spec_count']} specs, safe deal count {share['safe_deal_count']}, "
          f"scenario seed base {share['scenario_seed_base']}, batch seed {share['scenario_seed_base'] + BATCH_NUMBER_OF_EVERY_SHARE}"
          f"{pass_note}): current state {state[0]} ===", flush=True)
    if state[0] == STATE_COMPLETE:
        print(f"Already complete; a real run would skip it. {guidance_when_share_already_run(share)}")
        return
    with tempfile.TemporaryDirectory() as temporary_folder:
        scratch_corpus = Path(temporary_folder) / corpus_folder.name
        prepare_share_corpus_folder(share, scratch_corpus)
        command = batch_command(scratch_corpus, share, None) + ["--dry-run"]
        completed = subprocess.run(command, cwd=KIT_ROOT, capture_output=True, text=True)
        text = (completed.stdout + completed.stderr).replace(str(scratch_corpus), str(corpus_folder))
        print(text.replace(str(KIT_ROOT) + os.sep, ""))
        if completed.returncode != 0:
            raise SystemExit(f"Dry run failed for share {share['share_number']:03d}")


def keep_computer_awake_while_this_run_lasts() -> str:
    """On macOS, start `caffeinate` tied to this process, so the computer does not sleep while the run lasts (it stops by
    itself when this process exits). It prevents idle sleep, and system sleep while on mains power; it cannot stop the sleep
    that closing a laptop lid causes. Returns a message for the log."""
    caffeinate = shutil.which("caffeinate") if sys.platform == "darwin" else None
    if caffeinate is None:
        return ("Note: this computer is not kept awake automatically (caffeinate is macOS only). Turn off sleep while "
                "this runs. If it sleeps anyway, run the same command again to resume.")
    subprocess.Popen([caffeinate, "-i", "-m", "-s", "-w", str(os.getpid())])
    return ("Keeping this computer awake while the run lasts (caffeinate). Keep the lid open and the charger plugged in: "
            "closing the lid still puts a laptop to sleep. If it sleeps anyway, run the same command again to resume.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shares", required=True, help="comma-separated share numbers assigned to you, e.g. 7,12")
    parser.add_argument("--output-folder", type=Path, default=None,
                        help=f"default {DEFAULT_OUTPUT_FOLDER.name}/, or {DEFAULT_SECOND_PASS_OUTPUT_FOLDER.name}/ with --second-pass")
    parser.add_argument("--second-pass", action="store_true",
                        help="deal the same specs again with each share's seed base plus 500, into its own output folder")
    parser.add_argument("--scenario-seed-base", type=int, default=None,
                        help="explicit scenario seed base for exactly one share (needs its own --output-folder)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan for each share; create nothing")
    parser.add_argument("--status", action="store_true", help="show the state of each share; run nothing")
    parser.add_argument("--accept-other-login-method", action="store_true")
    parser.add_argument("--skip-login-check", action="store_true")
    arguments = parser.parse_args(argv)

    share_numbers = parse_share_numbers(arguments.shares)
    manifest = load_newest_manifest()
    shares = [find_share(manifest, number) for number in share_numbers]
    for share in shares:
        verified_share_file(share)
    shares, output_folder, zip_label = choose_seed_bases_and_output_folder(arguments, shares, manifest)

    if arguments.status:
        for share in shares:
            folder = corpus_folder_for_share(share["share_number"], output_folder, zip_label)
            print(f"Share {share['share_number']:03d}: {state_of_share_corpus(folder)[0] if folder.exists() else STATE_NOT_STARTED}")
        return 0
    check_python_and_packages()
    if arguments.dry_run:
        for share in shares:
            print_dry_run(share, output_folder, zip_label)
        print("\nDry run only: no model call was made and nothing was created.")
        return 0
    shares_already_complete = [share for share in shares
                               if corpus_folder_for_share(share["share_number"], output_folder, zip_label).exists()
                               and state_of_share_corpus(corpus_folder_for_share(share["share_number"], output_folder, zip_label))[0]
                               == STATE_COMPLETE]
    if len(shares_already_complete) == len(shares):
        print("Nothing was run: every share you asked for has already been run.", flush=True)
        for share in shares:
            print(guidance_when_share_already_run(share), flush=True)
        print(f"The zip from the earlier run is already in {relative_to_kit(ZIP_FOLDER)}/ ; no new zip was written.", flush=True)
        return ALREADY_RUN_EXIT_CODE

    if not arguments.skip_login_check:
        print(check_claude_login(arguments.accept_other_login_method), flush=True)

    print(keep_computer_awake_while_this_run_lasts(), flush=True)
    started = datetime.now()
    rerun_command = "python run_my_shares.py " + " ".join(shlex.quote(part) for part in (argv if argv is not None else sys.argv[1:]))
    for share in shares:
        code = run_share(share, output_folder, zip_label)
        if code == USAGE_LIMIT_EXIT_CODE:
            print(f"\nSTOPPED: your Claude usage limit was reached while working on share {share['share_number']:03d}. "
                  f"Nothing is lost. Wait for the limit to reset (the message above or Claude Code's /usage may say when), "
                  f"then run the same command again:\n    {rerun_command}", flush=True)
            return USAGE_LIMIT_EXIT_CODE
        if code != 0:
            print(f"\nSTOPPED: the batch for share {share['share_number']:03d} failed (exit code {code}); the message above "
                  f"and the terminal_output_*.txt files in {relative_to_kit(corpus_folder_for_share(share['share_number'], output_folder, zip_label))}"
                  f"/batches/ say why. Run the same command again to resume; if it fails again, send those files to the person "
                  f"who sent you the kit.", flush=True)
            return code
    zip_path = write_zip_of_share_corpora(share_numbers, output_folder, ZIP_FOLDER, label=zip_label)
    print(f"\nALL DONE in {(datetime.now() - started).total_seconds() / 60:.0f} minutes this session. "
          f"Send this file back: {relative_to_kit(zip_path)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
