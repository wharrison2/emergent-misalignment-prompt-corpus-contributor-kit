"""Keep only the prompts in which the user never mentions the animals' welfare, judged by Claude Haiku 5.5 through the Claude Code CLI.

Input (--prompts-file): a JSONL of prompts. The id is read from `prompt_gid`. The message is read from
`user_message`, falling back to `refined`; a record with neither field stops the run. The strictly implicit
difficult advice pipeline writes its final refined prompts to
outputs/dad/runs/<run>/step1/dilemmas.jsonl with `prompt_gid` and `user_message`.

Outputs in --output-folder (every file name ends with its date and time):
  welfare_mention_verdicts_<stamp>.jsonl        one record per judged prompt (resumable)
  welfare_mention_usage_log_<stamp>.jsonl       token usage and CLI-reported cost per call
  prompts_where_user_never_mentions_animal_welfare_<stamp>.jsonl   input records judged not_mentioned, unchanged
  welfare_mention_summary_<stamp>.json          counts per level

Resumption: prompts with an ok verdict in --resume-verdicts-file, or in any verdicts file already in the
output folder, are not judged again; unparseable ones are retried. Billed to the Claude subscription through
the CLI; there is no paid-API path in this script. Use --dry-run to render prompts and count calls without any model call.
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROMPT_DIRECTORY = SCRIPT_DIRECTORY / "prompts"
PROMPT_TEMPLATE_NAME = "judge_whether_user_mentions_animal_welfare_prompt_template.txt"
DEFAULT_JUDGE_MODEL = "claude-haiku-5-5"
PARALLEL_CALLS = 8
PROGRESS_REPORT_INTERVAL_SECONDS = 180
MAXIMUM_ATTEMPTS = 2  # one try plus one retry
LEVELS = ["not_mentioned", "mentioned_in_passing", "asked_about"]
NOT_MENTIONED = "not_mentioned"
USER_MESSAGE_FIELD_CANDIDATES = ["user_message", "refined"]
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": LEVELS},
        "evidence_quote": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["level", "evidence_quote", "note"],
    "additionalProperties": False,
}


def read_prompt_file(name: str) -> str:
    return (PROMPT_DIRECTORY / name).read_text(encoding="utf-8").rstrip("\n")


def get_user_message_text(prompt_record: dict) -> str:
    """Field fallback: `user_message` first, then `refined`. Fails loudly when neither is present."""
    for field_name in USER_MESSAGE_FIELD_CANDIDATES:
        value = prompt_record.get(field_name)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(f"record {prompt_record.get('prompt_gid')!r} has none of the message fields "
                     f"{USER_MESSAGE_FIELD_CANDIDATES} (keys present: {sorted(prompt_record)})")


def load_prompt_records(prompts_file: Path) -> list[dict]:
    records = []
    with open(prompts_file, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if not record.get("prompt_gid"):
                    raise ValueError(f"record without prompt_gid in {prompts_file}: keys {sorted(record)}")
                get_user_message_text(record)  # fail loudly now rather than mid-run
                records.append(record)
    return records


def render_judge_prompt(prompt_record: dict) -> str:
    return read_prompt_file(PROMPT_TEMPLATE_NAME).replace("{user_message}", get_user_message_text(prompt_record))


def check_reply(reply_text: str) -> tuple[dict | None, str | None]:
    """Return (verdict, None) when the reply is valid and consistent, else (None, reason)."""
    try:
        parsed = json.loads(reply_text)
    except (json.JSONDecodeError, TypeError):
        return None, "reply is not valid JSON"
    if not isinstance(parsed, dict):
        return None, "reply is not a JSON object"
    if set(parsed) != set(JSON_SCHEMA["required"]):
        return None, f"fields {sorted(parsed)} differ from expected {sorted(JSON_SCHEMA['required'])}"
    if parsed["level"] not in LEVELS:
        return None, f"level has value {parsed['level']!r} outside the schema"
    if not isinstance(parsed["evidence_quote"], str) or not isinstance(parsed["note"], str):
        return None, "evidence_quote and note must be strings"
    quote_is_empty = not parsed["evidence_quote"].strip()
    if parsed["level"] == NOT_MENTIONED and not quote_is_empty:
        return None, "evidence_quote is not empty although the level is not_mentioned"
    if parsed["level"] != NOT_MENTIONED and quote_is_empty:
        return None, "evidence_quote is empty although the level is not not_mentioned"
    return parsed, None


def judge_one_prompt(prompt_record: dict, call_model, judge_model=DEFAULT_JUDGE_MODEL, now=datetime.now):
    """Returns (verdict_record, usage_records). call_model(model, prompt, json_schema) -> ClaudeCodeReply-like."""
    prompt = render_judge_prompt(prompt_record)
    raw_replies, usage_records, verdict = [], [], None
    for attempt_number in range(1, MAXIMUM_ATTEMPTS + 1):
        reply = call_model(judge_model, prompt, JSON_SCHEMA)
        raw_replies.append(reply.reply_text)
        verdict, problem = check_reply(reply.reply_text)
        usage_records.append({
            "prompt_gid": prompt_record["prompt_gid"], "attempt": attempt_number, "judge_model": judge_model,
            "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
            "cache_read_input_tokens": reply.cache_read_input_tokens,
            "cache_creation_input_tokens": reply.cache_creation_input_tokens,
            "number_of_tools_loaded": reply.number_of_tools_loaded,
            "names_of_non_built_in_tools": list(reply.names_of_non_built_in_tools),
            "number_of_mcp_servers_loaded": reply.number_of_mcp_servers_loaded,
            "cli_reported_cost_usd": reply.reported_cost_usd, "problem": problem,
            "logged_at": now().isoformat(timespec="seconds")})
        if verdict is not None:
            break
    record = {"prompt_gid": prompt_record["prompt_gid"], "judge_model": judge_model,
              "level": verdict["level"] if verdict else None,
              "evidence_quote": verdict["evidence_quote"] if verdict else None,
              "note": verdict["note"] if verdict else None}
    record["evidence_quote_found_verbatim_in_message"] = (
        (verdict["evidence_quote"] in get_user_message_text(prompt_record)) if verdict and verdict["evidence_quote"] else None)
    record.update(parse_status="ok" if verdict else "unparseable", attempts=len(raw_replies),
                  input_tokens=sum(u["input_tokens"] for u in usage_records),
                  output_tokens=sum(u["output_tokens"] for u in usage_records),
                  cache_read_input_tokens=sum(u["cache_read_input_tokens"] for u in usage_records),
                  cache_creation_input_tokens=sum(u["cache_creation_input_tokens"] for u in usage_records),
                  cli_reported_cost_usd=sum(u["cli_reported_cost_usd"] or 0.0 for u in usage_records),
                  raw_replies=raw_replies, judged_at=now().isoformat(timespec="seconds"))
    return record, usage_records


def read_verdict_records(verdicts_files: list[Path]) -> list[dict]:
    records = []
    for path in verdicts_files:
        with open(path, encoding="utf-8") as handle:
            records += [json.loads(line) for line in handle if line.strip()]
    return records


def find_verdicts_files(output_folder: Path, resume_verdicts_file: Path | None) -> list[Path]:
    files = sorted(output_folder.glob("welfare_mention_verdicts_*.jsonl")) if output_folder.exists() else []
    if resume_verdicts_file is not None and resume_verdicts_file not in files:
        files.append(resume_verdicts_file)
    return files


def latest_verdict_per_prompt(verdict_records: list[dict]) -> dict[str, dict]:
    """Later records win, so a retried unparseable prompt is replaced by its new verdict."""
    return {record["prompt_gid"]: record for record in verdict_records}


def select_prompts_still_to_judge(prompt_records, verdict_records, maximum_prompts=None):
    done = {gid for gid, record in latest_verdict_per_prompt(verdict_records).items() if record["parse_status"] == "ok"}
    pending = [record for record in prompt_records if record["prompt_gid"] not in done]
    return pending[:maximum_prompts] if maximum_prompts is not None else pending


def write_final_outputs(prompt_records, verdict_records, output_folder: Path, stamp: str) -> dict:
    latest = latest_verdict_per_prompt(verdict_records)
    counts = {level: 0 for level in LEVELS}
    counts.update(unparseable=0, not_yet_judged=0)
    kept_records = []
    for record in prompt_records:
        verdict = latest.get(record["prompt_gid"])
        if verdict is None:
            counts["not_yet_judged"] += 1
        elif verdict["parse_status"] != "ok":
            counts["unparseable"] += 1
        else:
            counts[verdict["level"]] += 1
            if verdict["level"] == NOT_MENTIONED:
                kept_records.append(record)
    kept_path = output_folder / f"prompts_where_user_never_mentions_animal_welfare_{stamp}.jsonl"
    with open(kept_path, "w", encoding="utf-8") as handle:
        for record in kept_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary = {"total_prompts": len(prompt_records), "counts_per_level": counts, "kept_prompts": len(kept_records),
               "kept_prompts_file": kept_path.name}
    (output_folder / f"welfare_mention_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_filter(prompts_file: Path, output_folder: Path, call_model, resume_verdicts_file: Path | None = None,
               judge_model=DEFAULT_JUDGE_MODEL, maximum_prompts=None, now=datetime.now, parallel_calls=PARALLEL_CALLS,
               progress_interval_seconds=PROGRESS_REPORT_INTERVAL_SECONDS, log=print) -> dict:
    from claude_code_cli_structured_reply_without_paid_api_fallback import ClaudeCodeUsageLimitReached
    output_folder.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime(TIMESTAMP_FORMAT)
    prompt_records = load_prompt_records(prompts_file)
    existing_files = find_verdicts_files(output_folder, resume_verdicts_file)
    previous_verdicts = read_verdict_records(existing_files)
    pending = select_prompts_still_to_judge(prompt_records, previous_verdicts, maximum_prompts)
    if resume_verdicts_file is not None:
        verdict_path = resume_verdicts_file
    elif existing_files:
        verdict_path = existing_files[-1]
    else:
        verdict_path = output_folder / f"welfare_mention_verdicts_{stamp}.jsonl"
    usage_path = output_folder / f"welfare_mention_usage_log_{stamp}.jsonl"
    log(f"{len(prompt_records) - len(pending)} of {len(prompt_records)} prompts need no call; {len(pending)} to judge now, "
        f"appending to {verdict_path.name}")
    write_lock = threading.Lock()
    started = last_report = time.time()
    counts = {"judged": 0, "unparseable": 0, "stopped_for_usage_limit": False}
    total_cost = 0.0
    new_verdicts = []
    with open(verdict_path, "a", encoding="utf-8") as verdict_file, open(usage_path, "a", encoding="utf-8") as usage_file, \
            ThreadPoolExecutor(max_workers=parallel_calls) as pool:
        futures = [pool.submit(judge_one_prompt, record, call_model, judge_model, now) for record in pending]
        for future in as_completed(futures):
            try:
                verdict_record, usage_records = future.result()
            except ClaudeCodeUsageLimitReached as error:
                if not counts["stopped_for_usage_limit"]:
                    log(f"Usage limit reached ({error}); stopping. Rerun to resume.")
                counts["stopped_for_usage_limit"] = True
                for other in futures:
                    other.cancel()
                continue
            except Exception as error:  # one failed call must not lose the rest; the prompt stays unjudged and is retried on resume
                log(f"Call failed for one prompt, left for resume: {error}")
                continue
            with write_lock:
                verdict_file.write(json.dumps(verdict_record, ensure_ascii=False) + "\n")
                verdict_file.flush()
                for usage_record in usage_records:
                    usage_file.write(json.dumps(usage_record) + "\n")
                    total_cost += usage_record["cli_reported_cost_usd"] or 0.0
                usage_file.flush()
            new_verdicts.append(verdict_record)
            counts["judged"] += 1
            counts["unparseable"] += verdict_record["parse_status"] == "unparseable"
            if time.time() - last_report >= progress_interval_seconds:
                last_report = time.time()
                log(f"[{(last_report - started) / 60:.1f} min] judged {counts['judged']}/{len(pending)}, "
                    f"unparseable {counts['unparseable']}, CLI-reported cost so far ${total_cost:.4f}")
    log(f"Done in {(time.time() - started) / 60:.1f} min: judged {counts['judged']}, unparseable {counts['unparseable']}, "
        f"CLI-reported cost ${total_cost:.4f}")
    summary = write_final_outputs(prompt_records, previous_verdicts + new_verdicts, output_folder, stamp)
    summary.update(counts, total_cli_reported_cost_usd=total_cost)
    (output_folder / f"welfare_mention_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"Counts per level: {summary['counts_per_level']}; kept {summary['kept_prompts']} prompts")
    return summary


def dry_run(prompts_file: Path, output_folder: Path, resume_verdicts_file=None, maximum_prompts=None,
            examples_to_print=2, log=print) -> int:
    prompt_records = load_prompt_records(prompts_file)
    previous = read_verdict_records(find_verdicts_files(output_folder, resume_verdicts_file))
    pending = select_prompts_still_to_judge(prompt_records, previous, maximum_prompts)
    for record in pending[:examples_to_print]:
        log(f"\n----- DRY RUN prompt for {record['prompt_gid']} -----")
        log(render_judge_prompt(record))
    log(f"\nDRY RUN: {len(pending)} prompts to judge = {len(pending)} model calls (more only if retries are needed). "
        f"No model call was made.")
    return len(pending)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompts-file", required=True, type=Path)
    parser.add_argument("--output-folder", required=True, type=Path)
    parser.add_argument("--resume-verdicts-file", type=Path, default=None,
                        help="verdicts file to resume from and append to (default: the newest one already in the output folder)")
    parser.add_argument("--dry-run", action="store_true", help="render prompts and count calls without any model call")
    parser.add_argument("--maximum-prompts", type=int, default=None)
    parser.add_argument("--parallel-calls", type=int, default=PARALLEL_CALLS,
                        help=f"number of Claude Code CLI calls running at once (default {PARALLEL_CALLS})")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help=f"judge model (default {DEFAULT_JUDGE_MODEL})")
    arguments = parser.parse_args(argv)
    if arguments.dry_run:
        dry_run(arguments.prompts_file, arguments.output_folder, arguments.resume_verdicts_file, arguments.maximum_prompts)
        return
    from claude_code_cli_structured_reply_without_paid_api_fallback import call_claude_code_cli_for_structured_reply

    def call_model(model, prompt, json_schema):
        return call_claude_code_cli_for_structured_reply(model, prompt, json_schema)

    run_filter(arguments.prompts_file, arguments.output_folder, call_model, arguments.resume_verdicts_file,
               arguments.judge_model, arguments.maximum_prompts, parallel_calls=arguments.parallel_calls)


if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
    main()
