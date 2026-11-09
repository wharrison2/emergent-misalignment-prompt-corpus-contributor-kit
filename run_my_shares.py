"""Run your assigned shares of the emergent misalignment prompt corpus, one batch per share, and zip the results.

    python run_my_shares.py --shares 7,12

For each share this creates (or reuses) my_share_corpora/contributor_corpus_share_NNN/ with a settings file naming the
share's spec set file and scenario seed base, then runs ONE batch there that deals the share's safe number of specs
(add_batch_to_emergent_misalignment_prompt_corpus.py does the work: scenario generation, drafting and gating through
Claude Code, then two judges). Run it again with the same command after any stop: a finished share is skipped and an
unfinished batch is resumed from its checkpoints. When every share you asked for is complete, it writes one zip in
zips_to_send_back/ named with the share numbers and the date and time. Send that zip back.

All model calls go through the Claude Code command line tool, billed to your Claude subscription. No API key is read or
needed. If your usage limit is reached the run stops cleanly (exit code 3): wait for the limit to reset and rerun.

--dry-run prints what would run for each share and creates nothing in your output folder. --status prints the state of
each share. Neither makes a model call.
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
from datetime import datetime
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent
BATCH_SCRIPT = KIT_ROOT / "scripts" / "emergent_misalignment" / "add_batch_to_emergent_misalignment_prompt_corpus.py"
SHARE_FOLDER = KIT_ROOT / "contributor_shares"
MANIFEST_PATTERN = "contributor_share_manifest_*.json"
DEFAULT_OUTPUT_FOLDER = KIT_ROOT / "my_share_corpora"
ZIP_FOLDER = KIT_ROOT / "zips_to_send_back"
CORPUS_SETTINGS_FILE_NAME = "corpus_settings_scenario_seed_base_and_spec_set_file.json"
LEDGER_FILE_NAME = "corpus_batch_ledger_started_2026-11-06.jsonl"
LOCK_FILE_NAME = "run_my_shares_lock_while_a_run_is_in_progress.json"
BATCH_FOLDER_PREFIX = "batch_"
USAGE_LIMIT_EXIT_CODE = 3
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"
MINIMUM_PYTHON = (3, 10)
SUBSCRIPTION_AUTH_METHOD = "claude.ai"

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


def corpus_folder_for_share(share_number: int, output_folder: Path) -> Path:
    return Path(output_folder) / f"contributor_corpus_share_{share_number:03d}"


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
                             f"Move that folder away and rerun.")
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
                               now=datetime.now) -> Path:
    zip_folder.mkdir(parents=True, exist_ok=True)
    share_text = "_".join(f"{number:03d}" for number in sorted(share_numbers))
    zip_path = zip_folder / (f"emergent_misalignment_contributor_corpus_shares_{share_text}_"
                             f"{now().strftime(TIMESTAMP_FORMAT)}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for number in sorted(share_numbers):
            corpus_folder = corpus_folder_for_share(number, output_folder)
            for path in sorted(corpus_folder.rglob("*")):
                if path.is_file() and path.name != LOCK_FILE_NAME:
                    archive.write(path, Path(corpus_folder.name) / path.relative_to(corpus_folder))
    return zip_path


# ---------- main ----------

def run_share(share: dict, output_folder: Path) -> int:
    """Run (or resume) the share's one batch. Returns 0 when the share is complete, else the batch script's exit code."""
    number = share["share_number"]
    corpus_folder = corpus_folder_for_share(number, output_folder)
    prepare_share_corpus_folder(share, corpus_folder)
    state, unfinished = state_of_share_corpus(corpus_folder)
    if state == STATE_COMPLETE:
        print(f"Share {number:03d}: already complete, skipping.", flush=True)
        return 0
    lock_path = take_lock(corpus_folder)
    try:
        action = f"resuming {relative_to_kit(unfinished)}" if unfinished else f"starting a batch of {share['safe_deal_count']} specs"
        print(f"[{datetime.now():%H:%M:%S}] Share {number:03d}: {action}. Progress is reported about every 3 minutes.", flush=True)
        return subprocess.call(batch_command(corpus_folder, share, unfinished), cwd=KIT_ROOT)
    finally:
        lock_path.unlink(missing_ok=True)


def print_dry_run(share: dict, output_folder: Path) -> None:
    """Show the batch script's own dry run for the share, in a throwaway corpus folder, so nothing is created in yours."""
    corpus_folder = corpus_folder_for_share(share["share_number"], output_folder)
    state = state_of_share_corpus(corpus_folder) if corpus_folder.exists() else (STATE_NOT_STARTED, None)
    print(f"\n=== Share {share['share_number']:03d} ({share['spec_count']} specs, safe deal count {share['safe_deal_count']}, "
          f"scenario seed base {share['scenario_seed_base']}): current state {state[0]} ===", flush=True)
    if state[0] == STATE_COMPLETE:
        print("Already complete; a real run would skip it.")
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
    parser.add_argument("--output-folder", type=Path, default=DEFAULT_OUTPUT_FOLDER)
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
    output_folder = arguments.output_folder.resolve()

    if arguments.status:
        for share in shares:
            folder = corpus_folder_for_share(share["share_number"], output_folder)
            print(f"Share {share['share_number']:03d}: {state_of_share_corpus(folder)[0] if folder.exists() else STATE_NOT_STARTED}")
        return 0
    check_python_and_packages()
    if arguments.dry_run:
        for share in shares:
            print_dry_run(share, output_folder)
        print("\nDry run only: no model call was made and nothing was created.")
        return 0
    if not arguments.skip_login_check:
        print(check_claude_login(arguments.accept_other_login_method), flush=True)

    print(keep_computer_awake_while_this_run_lasts(), flush=True)
    started = datetime.now()
    for share in shares:
        code = run_share(share, output_folder)
        if code == USAGE_LIMIT_EXIT_CODE:
            print(f"\nSTOPPED: your Claude usage limit was reached while working on share {share['share_number']:03d}. "
                  f"Nothing is lost. Wait for the limit to reset (the message above or Claude Code's /usage may say when), "
                  f"then run the same command again:\n    python run_my_shares.py --shares {arguments.shares}", flush=True)
            return USAGE_LIMIT_EXIT_CODE
        if code != 0:
            print(f"\nSTOPPED: the batch for share {share['share_number']:03d} failed (exit code {code}); the message above "
                  f"and the terminal_output_*.txt files in {relative_to_kit(corpus_folder_for_share(share['share_number'], output_folder))}"
                  f"/batches/ say why. Run the same command again to resume; if it fails again, send those files to the person "
                  f"who sent you the kit.", flush=True)
            return code
    zip_path = write_zip_of_share_corpora(share_numbers, output_folder)
    print(f"\nALL DONE in {(datetime.now() - started).total_seconds() / 60:.0f} minutes this session. "
          f"Send this file back: {relative_to_kit(zip_path)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
