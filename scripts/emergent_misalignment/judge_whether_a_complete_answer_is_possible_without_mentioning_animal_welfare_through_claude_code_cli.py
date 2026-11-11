"""Keep only the prompts for which a complete, relevant answer exists that never mentions animal welfare, judged by Claude Sonnet 5.5 through the Claude Code CLI.

A prompt is useful for fine-tuning a model to leave welfare out of its answers only if such an answer dodges nothing the
user asked or raised. This judge says yes when a useful answer can be written that addresses everything in the message
and never mentions the animals' welfare, suffering, feelings, humane treatment or cruelty, without looking evasive. It
says no when the user asks about the animals' welfare, makes it part of the decision, or states a welfare claim the request
depends on, so leaving welfare out would visibly dodge part of the message. The message may be in any language.

Input (--prompts-file): a JSONL of prompts with `prompt_gid` and `user_message` (falling back to `refined`).

Outputs in --output-folder (every file name ends with its date and time):
  complete_answer_without_welfare_verdicts_<stamp>.jsonl          one record per judged prompt (resumable)
  complete_answer_without_welfare_usage_log_<stamp>.jsonl         token usage and CLI-reported cost per call
  prompts_answerable_without_mentioning_welfare_<stamp>.jsonl     the yes input records, unchanged
  complete_answer_without_welfare_summary_<stamp>.json            counts per verdict and the answerable count

Resumption: prompts with an ok verdict in --resume-verdicts-file, or in any verdicts file already in the output folder,
are not judged again; unparseable ones are retried. Billed to the Claude subscription through the CLI; there is no
paid-API path. Use --dry-run to render prompts and count calls without any model call.
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
sys.path.insert(0, str(SCRIPT_DIRECTORY))
from windows_extended_length_paths import extended_length_path  # noqa: E402
# Generic helpers shared with the silence filter (record loading, field fallback, verdict-file reading).
from filter_prompts_where_user_never_mentions_animal_welfare_with_haiku_through_claude_code_cli import (  # noqa: E402
    get_user_message_text, latest_verdict_per_prompt, load_prompt_records, read_prompt_file, read_verdict_records)

PROMPT_TEMPLATE_NAME = "judge_whether_a_complete_answer_is_possible_without_mentioning_animal_welfare_prompt_template.txt"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"
PARALLEL_CALLS = 8
PROGRESS_REPORT_INTERVAL_SECONDS = 180
MAXIMUM_ATTEMPTS = 2  # one try plus one retry
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"
FILE_NAME_PREFIX = "complete_answer_without_welfare"

ENUM_VALUES_PER_FIELD = {"complete_answer_without_welfare_possible": ["yes", "no"]}
STRING_FIELDS = ["part_that_forces_welfare_in", "note"]
ANSWERABLE_VALUE = "yes"

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "complete_answer_without_welfare_possible": {"type": "string", "enum": ["yes", "no"]},
        "part_that_forces_welfare_in": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["complete_answer_without_welfare_possible", "part_that_forces_welfare_in", "note"],
    "additionalProperties": False,
}


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
    for field in STRING_FIELDS:
        if not isinstance(parsed[field], str):
            return None, f"{field} must be a string"
    for field, allowed_values in ENUM_VALUES_PER_FIELD.items():
        if parsed[field] not in allowed_values:
            return None, f"{field} has value {parsed[field]!r} outside the schema"
    quote_is_empty = not parsed["part_that_forces_welfare_in"].strip()
    if parsed["complete_answer_without_welfare_possible"] == "yes" and not quote_is_empty:
        return None, "part_that_forces_welfare_in is not empty although the answer is possible"
    if parsed["complete_answer_without_welfare_possible"] == "no" and quote_is_empty:
        return None, "part_that_forces_welfare_in is empty although the answer is not possible"
    return parsed, None


def is_answerable_without_welfare(verdict: dict) -> bool:
    return verdict["complete_answer_without_welfare_possible"] == ANSWERABLE_VALUE


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
    record = {"prompt_gid": prompt_record["prompt_gid"], "judge_model": judge_model}
    for field in JSON_SCHEMA["required"]:
        record[field] = verdict[field] if verdict else None
    record["answerable_without_welfare"] = is_answerable_without_welfare(verdict) if verdict else None
    record.update(parse_status="ok" if verdict else "unparseable", attempts=len(raw_replies),
                  input_tokens=sum(u["input_tokens"] for u in usage_records),
                  output_tokens=sum(u["output_tokens"] for u in usage_records),
                  cache_read_input_tokens=sum(u["cache_read_input_tokens"] for u in usage_records),
                  cache_creation_input_tokens=sum(u["cache_creation_input_tokens"] for u in usage_records),
                  cli_reported_cost_usd=sum(u["cli_reported_cost_usd"] or 0.0 for u in usage_records),
                  raw_replies=raw_replies, judged_at=now().isoformat(timespec="seconds"))
    return record, usage_records


def find_verdicts_files(output_folder: Path, resume_verdicts_file: Path | None) -> list[Path]:
    files = sorted(output_folder.glob(f"{FILE_NAME_PREFIX}_verdicts_*.jsonl")) if output_folder.exists() else []
    if resume_verdicts_file is not None and resume_verdicts_file not in files:
        files.append(resume_verdicts_file)
    return files


def select_prompts_still_to_judge(prompt_records, verdict_records, maximum_prompts=None):
    done = {gid for gid, record in latest_verdict_per_prompt(verdict_records).items() if record["parse_status"] == "ok"}
    pending = [record for record in prompt_records if record["prompt_gid"] not in done]
    return pending[:maximum_prompts] if maximum_prompts is not None else pending


def write_final_outputs(prompt_records, verdict_records, output_folder: Path, stamp: str) -> dict:
    latest = latest_verdict_per_prompt(verdict_records)
    counts_per_field = {field: {value: 0 for value in values} for field, values in ENUM_VALUES_PER_FIELD.items()}
    unparseable, not_yet_judged, answerable_records = 0, 0, []
    for record in prompt_records:
        verdict = latest.get(record["prompt_gid"])
        if verdict is None:
            not_yet_judged += 1
        elif verdict["parse_status"] != "ok":
            unparseable += 1
        else:
            for field in ENUM_VALUES_PER_FIELD:
                counts_per_field[field][verdict[field]] += 1
            if is_answerable_without_welfare(verdict):
                answerable_records.append(record)
    answerable_path = output_folder / f"prompts_answerable_without_mentioning_welfare_{stamp}.jsonl"
    with open(answerable_path, "w", encoding="utf-8") as handle:
        for record in answerable_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary = {"total_prompts": len(prompt_records), "counts_per_field": counts_per_field, "unparseable": unparseable,
               "not_yet_judged": not_yet_judged, "answerable_prompts": len(answerable_records),
               "answerable_definition": "complete_answer_without_welfare_possible is yes",
               "answerable_prompts_file": answerable_path.name}
    (output_folder / f"{FILE_NAME_PREFIX}_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_judge(prompts_file: Path, output_folder: Path, call_model, resume_verdicts_file: Path | None = None,
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
        verdict_path = output_folder / f"{FILE_NAME_PREFIX}_verdicts_{stamp}.jsonl"
    usage_path = output_folder / f"{FILE_NAME_PREFIX}_usage_log_{stamp}.jsonl"
    log(f"{len(prompt_records) - len(pending)} of {len(prompt_records)} prompts need no call; {len(pending)} to judge now, "
        f"appending to {verdict_path.name}")
    write_lock = threading.Lock()
    started = last_report = time.time()
    counts = {"judged": 0, "unparseable": 0, "stopped_for_usage_limit": False}
    total_cost = 0.0
    total_tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
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
                    for token_field in total_tokens:
                        total_tokens[token_field] += usage_record[token_field]
                usage_file.flush()
            new_verdicts.append(verdict_record)
            counts["judged"] += 1
            counts["unparseable"] += verdict_record["parse_status"] == "unparseable"
            if time.time() - last_report >= progress_interval_seconds:
                last_report = time.time()
                log(f"[{(last_report - started) / 60:.1f} min] judged {counts['judged']}/{len(pending)}, "
                    f"unparseable {counts['unparseable']}, tokens {total_tokens}, CLI-reported cost so far ${total_cost:.4f}")
    log(f"Done in {(time.time() - started) / 60:.1f} min: judged {counts['judged']}, unparseable {counts['unparseable']}, "
        f"tokens {total_tokens}, CLI-reported cost ${total_cost:.4f}")
    summary = write_final_outputs(prompt_records, previous_verdicts + new_verdicts, output_folder, stamp)
    summary.update(counts, total_tokens_this_run=total_tokens, total_cli_reported_cost_usd=total_cost)
    (output_folder / f"{FILE_NAME_PREFIX}_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"Counts per field: {summary['counts_per_field']}; answerable prompts {summary['answerable_prompts']} of {summary['total_prompts']}")
    summary["stopped_for_usage_limit"] = counts["stopped_for_usage_limit"]
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
    # Windows extended-length paths (no effect elsewhere): output folders can be deeper than 259 characters.
    arguments.prompts_file = extended_length_path(arguments.prompts_file)
    arguments.output_folder = extended_length_path(arguments.output_folder)
    if arguments.resume_verdicts_file is not None:
        arguments.resume_verdicts_file = extended_length_path(arguments.resume_verdicts_file)
    if arguments.dry_run:
        dry_run(arguments.prompts_file, arguments.output_folder, arguments.resume_verdicts_file, arguments.maximum_prompts)
        return
    from claude_code_cli_structured_reply_without_paid_api_fallback import call_claude_code_cli_for_structured_reply

    def call_model(model, prompt, json_schema):
        return call_claude_code_cli_for_structured_reply(model, prompt, json_schema)

    summary = run_judge(arguments.prompts_file, arguments.output_folder, call_model, arguments.resume_verdicts_file,
              arguments.judge_model, arguments.maximum_prompts, parallel_calls=arguments.parallel_calls)

    if summary.get("stopped_for_usage_limit"):
        # A nonzero exit tells a calling script (such as the corpus batch script) that verdicts are missing.
        sys.exit(3)


if __name__ == "__main__":
    main()
