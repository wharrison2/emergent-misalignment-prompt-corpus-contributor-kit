"""Shared helpers for the control prompt rewrite work: prompt rendering and reply checks, with no model call.

A control prompt is a rewrite of a treatment prompt (a user message with an implicit animal welfare stake) that removes the
stake and keeps the person, the setting and the animals. This module holds what both the pilot experiment script
(experiment_with_control_prompt_rewrite_strategies_that_remove_the_animal_welfare_stake_through_claude_code_cli.py) and the
contributor kit's batch script (add_control_rewrite_batch_to_emergent_misalignment_prompt_corpus.py) need:
  * rendering the rewrite prompt (base template, strategy instructions, word count placeholders), the no-stake judge prompt
    and the animal role prompt;
  * checking the three replies: rewrite (check_rewrite_reply), no-stake judge (check_judge_reply) and animal role label
    (check_animal_role_reply);
  * the two pass rules of the no-stake judge, passes_strict and passes_allowing_background_harm.
The kit ships this module, not the experiment script. Moved out of the experiment script unchanged, except that
render_rewrite_prompt reads practical_advantage only when the chosen template uses it (the kit's shares do not carry it).
"""

import json
import re
import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))
from filter_prompts_where_user_never_mentions_animal_welfare_with_haiku_through_claude_code_cli import (  # noqa: E402
    get_user_message_text, read_prompt_file)

ORIGINAL_REWRITE_BASE_TEMPLATE_NAME = "control_prompt_rewrite_base_prompt_template.txt"
STRATEGY_INSTRUCTIONS_NAME_PATTERN = "control_prompt_rewrite_strategy_instructions__{strategy}.txt"
NO_STAKE_JUDGE_TEMPLATE_NAME = "judge_whether_prompt_carries_no_animal_welfare_stake_prompt_template.txt"
# Every accepted strategy and the base template it is rendered with. Add a later version with one new entry here.
BASE_TEMPLATE_FILE_NAME_PER_STRATEGY = {
    "smallest_change_of_the_rewriters_choice": ORIGINAL_REWRITE_BASE_TEMPLATE_NAME,
    "change_the_harmful_detail_and_keep_the_practice": ORIGINAL_REWRITE_BASE_TEMPLATE_NAME,
    "replace_the_practice_and_keep_the_animals": ORIGINAL_REWRITE_BASE_TEMPLATE_NAME,
    "replace_the_practice_and_keep_the_animals_version_2": "control_prompt_rewrite_base_prompt_template_version_2.txt",
    "replace_the_practice_and_keep_the_animals_version_3": "control_prompt_rewrite_base_prompt_template_version_3.txt",
    "replace_the_practice_and_keep_the_animals_version_4": "control_prompt_rewrite_base_prompt_template_version_4.txt",
    "replace_the_practice_and_keep_the_animals_version_5": "control_prompt_rewrite_base_prompt_template_version_5.txt",
    "replace_the_practice_and_keep_the_animals_version_6": "control_prompt_rewrite_base_prompt_template_version_6.txt",
    "replace_the_practice_and_keep_the_animals_version_7": "control_prompt_rewrite_base_prompt_template_version_7.txt",
    "replace_the_practice_and_keep_the_animals_version_8": "control_prompt_rewrite_base_prompt_template_version_8.txt",
    "replace_the_practice_and_keep_the_animals_version_9": "control_prompt_rewrite_base_prompt_template_version_9.txt",
}
ANIMAL_ROLE_TEMPLATE_NAME = "judge_whether_animals_keep_a_real_place_in_the_request_prompt_template_version_1.txt"

REWRITE_FIELDS = ["can_be_rewritten", "what_was_changed", "rewritten_user_message"]
REWRITE_JSON_SCHEMA = {
    "type": "object",
    "properties": {"can_be_rewritten": {"type": "string", "enum": ["yes", "no"]},
                   "what_was_changed": {"type": "string"}, "rewritten_user_message": {"type": "string"}},
    "required": REWRITE_FIELDS, "additionalProperties": False}

JUDGE_ENUM_VALUES_PER_FIELD = {
    "harm_from_the_request_evident": ["clearly", "only_on_reflection", "not_evident"],
    "background_harm_in_the_setting": ["yes", "no"],
    "welfare_weighing_advisor_would_answer_differently": ["yes", "no"],
    "good_answer_would_raise_animal_welfare": ["yes", "no"],
    "user_mentions_animal_welfare": ["yes", "no"],
}
JUDGE_STRING_FIELDS = ["animals_in_the_message", "harm_from_the_request", "best_answer_ignoring_welfare", "note"]
JUDGE_FIELDS = ["animals_in_the_message", "harm_from_the_request", "harm_from_the_request_evident",
                "background_harm_in_the_setting", "best_answer_ignoring_welfare",
                "welfare_weighing_advisor_would_answer_differently", "good_answer_would_raise_animal_welfare",
                "user_mentions_animal_welfare", "note"]
JUDGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {**{field: {"type": "string"} for field in JUDGE_STRING_FIELDS},
                   **{field: {"type": "string", "enum": values} for field, values in JUDGE_ENUM_VALUES_PER_FIELD.items()}},
    "required": JUDGE_FIELDS, "additionalProperties": False}

ANIMAL_ROLE_VALUES = ["subject", "supporting", "scenery"]
ANIMAL_ROLE_STRING_FIELDS = ["animals_in_the_message", "request_in_one_sentence", "what_removing_the_animals_would_lose"]
ANIMAL_ROLE_FIELDS = ANIMAL_ROLE_STRING_FIELDS + ["animal_role"]
ANIMAL_ROLE_JSON_SCHEMA = {
    "type": "object",
    "properties": {**{field: {"type": "string"} for field in ANIMAL_ROLE_STRING_FIELDS},
                   "animal_role": {"type": "string", "enum": ANIMAL_ROLE_VALUES}},
    "required": ANIMAL_ROLE_FIELDS, "additionalProperties": False}


# ---------------------------------------------------------------- prompt rendering

def fill_placeholders_in_one_pass(template: str, values: dict) -> str:
    """Substitute every known {placeholder} in a single left-to-right pass, so text that was substituted in (a user message
    containing '{harm_type}', say) is never scanned again and stays exactly as written."""
    pattern = re.compile("{(" + "|".join(re.escape(name) for name in values) + ")}")
    return pattern.sub(lambda match: values[match.group(1)], template)


def render_rewrite_prompt(source_prompt: dict, strategy: str) -> str:
    instructions = read_prompt_file(STRATEGY_INSTRUCTIONS_NAME_PATTERN.format(strategy=strategy))
    base_template = read_prompt_file(BASE_TEMPLATE_FILE_NAME_PER_STRATEGY[strategy])
    source_user_message = get_user_message_text(source_prompt)
    original_word_count = len(source_user_message.split())
    values = {
        "harm_type": str(source_prompt["harm_type"]),
        "taxa_category": str(source_prompt["taxa_category"]), "taxa_subcategory": str(source_prompt["taxa_subcategory"]),
        "strategy_instructions": instructions, "user_message": source_user_message,
        "original_word_count": str(original_word_count),
        "original_word_count_lower_bound": str(round(0.9 * original_word_count)),
        "original_word_count_upper_bound": str(round(1.1 * original_word_count))}
    # Only the older templates use the practical advantage; a record without it fails loudly if the template needs it.
    if "practical_advantage" in source_prompt or "{practical_advantage}" in base_template:
        values["practical_advantage"] = str(source_prompt["practical_advantage"])
    return fill_placeholders_in_one_pass(base_template, values)


def render_judge_prompt(message_text: str, judge_template_file: str = NO_STAKE_JUDGE_TEMPLATE_NAME) -> str:
    return fill_placeholders_in_one_pass(read_prompt_file(judge_template_file), {"user_message": message_text})


def render_animal_role_prompt(message_text: str, template_file: str = ANIMAL_ROLE_TEMPLATE_NAME) -> str:
    return fill_placeholders_in_one_pass(read_prompt_file(template_file), {"user_message": message_text})


# ---------------------------------------------------------------- reply checks

def parse_json_object_with_exact_fields(reply_text: str, required_fields: list[str]):
    try:
        parsed = json.loads(reply_text)
    except (json.JSONDecodeError, TypeError):
        return None, "reply is not valid JSON"
    if not isinstance(parsed, dict):
        return None, "reply is not a JSON object"
    if set(parsed) != set(required_fields):
        return None, f"fields {sorted(parsed)} differ from expected {sorted(required_fields)}"
    return parsed, None


def check_rewrite_reply(reply_text: str) -> tuple[dict | None, str | None]:
    parsed, problem = parse_json_object_with_exact_fields(reply_text, REWRITE_FIELDS)
    if parsed is None:
        return None, problem
    if parsed["can_be_rewritten"] not in ("yes", "no"):
        return None, f"can_be_rewritten has value {parsed['can_be_rewritten']!r} outside the schema"
    for field in ("what_was_changed", "rewritten_user_message"):
        if not isinstance(parsed[field], str):
            return None, f"{field} must be a string"
    if not parsed["what_was_changed"].strip():
        return None, "what_was_changed is empty"
    rewrite_is_empty = not parsed["rewritten_user_message"].strip()
    if parsed["can_be_rewritten"] == "yes" and rewrite_is_empty:
        return None, "rewritten_user_message is empty although can_be_rewritten is yes"
    if parsed["can_be_rewritten"] == "no" and not rewrite_is_empty:
        return None, "rewritten_user_message is not empty although can_be_rewritten is no"
    return parsed, None


def check_judge_reply(reply_text: str) -> tuple[dict | None, str | None]:
    parsed, problem = parse_json_object_with_exact_fields(reply_text, JUDGE_FIELDS)
    if parsed is None:
        return None, problem
    for field in JUDGE_STRING_FIELDS:
        if not isinstance(parsed[field], str):
            return None, f"{field} must be a string"
    for field, allowed_values in JUDGE_ENUM_VALUES_PER_FIELD.items():
        if parsed[field] not in allowed_values:
            return None, f"{field} has value {parsed[field]!r} outside the schema"
    harm_is_empty = not parsed["harm_from_the_request"].strip()
    if parsed["harm_from_the_request_evident"] == "not_evident" and not harm_is_empty:
        # The judge sometimes explains the absence of harm in this field; keep that text apart rather than failing the parse.
        parsed["harm_from_the_request_text_given_although_not_evident"] = parsed["harm_from_the_request"]
        parsed["harm_from_the_request"] = ""
        harm_is_empty = True
    if parsed["harm_from_the_request_evident"] != "not_evident" and harm_is_empty:
        return None, "harm_from_the_request is empty although the harm is evident at least on reflection"
    if not parsed["animals_in_the_message"].strip():
        if parsed["harm_from_the_request_evident"] != "not_evident":
            return None, "no animals listed but harm_from_the_request_evident is not not_evident"
        if parsed["background_harm_in_the_setting"] != "no":
            return None, "no animals listed but background_harm_in_the_setting is not no"
    return parsed, None


def check_animal_role_reply(reply_text: str) -> tuple[dict | None, str | None]:
    """The animal role label: four fields, animal_role one of subject, supporting, scenery, and scenery when no animals are
    listed. A label only: nothing is accepted or rejected on it."""
    parsed, problem = parse_json_object_with_exact_fields(reply_text, ANIMAL_ROLE_FIELDS)
    if parsed is None:
        return None, problem
    for field in ANIMAL_ROLE_STRING_FIELDS:
        if not isinstance(parsed[field], str):
            return None, f"{field} must be a string"
    if parsed["animal_role"] not in ANIMAL_ROLE_VALUES:
        return None, f"animal_role has value {parsed['animal_role']!r} outside the schema"
    for field in ("request_in_one_sentence", "what_removing_the_animals_would_lose"):
        if not parsed[field].strip():
            return None, f"{field} is empty"
    if not parsed["animals_in_the_message"].strip() and parsed["animal_role"] != "scenery":
        return None, "no animals listed but animal_role is not scenery"
    return parsed, None


def passes_strict(verdict: dict) -> bool:
    return (passes_allowing_background_harm(verdict) and verdict["background_harm_in_the_setting"] == "no")


def passes_allowing_background_harm(verdict: dict) -> bool:
    """Only harm the judge marks "clearly" fails a prompt on this field: harm marked only_on_reflection does not fail it on
    its own (the judge used that answer as a hedge, its note saying nothing changes for the animals, on rewrites the reader
    and hand labels passed). It still fails when the judge also says a welfare-weighing advisor would answer differently
    or would raise welfare."""
    return (verdict["harm_from_the_request_evident"] != "clearly"
            and bool(verdict["animals_in_the_message"].strip())
            and verdict["welfare_weighing_advisor_would_answer_differently"] == "no"
            and verdict["good_answer_would_raise_animal_welfare"] == "no"
            and verdict["user_mentions_animal_welfare"] == "no")
