"""One structured-output model call through the Claude Code CLI (claude_agent_sdk), subscription only.

Adapted from `_run_claude_code_query` in
animal-welfare-data-pipeline-emergent-misalignment-prompts/shared/api.py (see references.md).
Differences from the original: no `auto` or `api` backend exists here at all, so there is
nothing to fall back to; no global state; no tenacity; the paid-API environment variables
are blanked and a refusal to run is raised if the SDK reports an API-key billing source.
Sampling parameters are never set.
"""

from dataclasses import dataclass


class ClaudeCodeUsageLimitReached(Exception):
    """Subscription window exhausted. Not retried: stop and resume later."""


class ClaudeCodeCallFailed(Exception):
    """Any other failure of the CLI call."""


@dataclass
class ClaudeCodeReply:
    reply_text: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    reported_cost_usd: float | None
    # What the CLI reported loading at session start (the system/init message); None when no init message arrived.
    number_of_tools_loaded: int | None = None
    names_of_non_built_in_tools: tuple[str, ...] = ()
    number_of_mcp_servers_loaded: int | None = None
    number_of_turns: int | None = None


NEUTRAL_SYSTEM_PROMPT = "You are a careful evaluator. Follow the instructions in the user message exactly."
USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "limit will reset")


def build_judge_options(model: str, json_schema: dict, shut_out_claude_ai_connectors: bool = True):
    """The ClaudeAgentOptions of one judge call. `shut_out_claude_ai_connectors=False` reproduces the old
    options and exists only so the context bloat diagnostic can compare against them."""
    from claude_agent_sdk import ClaudeAgentOptions

    # Empty values make the CLI treat these as unset, so it uses its subscription login
    # and can never bill an API key found in the environment.
    environment = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}
    connector_options = {}
    if shut_out_claude_ai_connectors:
        # The CLI otherwise fetches the account's claude.ai connectors (Slack, Gmail, Google Docs, ...) at
        # random points during start-up, which adds a cached tool-definition block of about 32,000 tokens to
        # roughly a quarter of calls. `tools=[]` and `setting_sources=[]` do not stop that; an empty server
        # set with strict mode (plus the environment variable) does. An empty `mcp_servers` adds no
        # `--mcp-config`; `strict_mcp_config` adds `--strict-mcp-config`.
        environment["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        connector_options = {"mcp_servers": {}, "strict_mcp_config": True}
    return ClaudeAgentOptions(
        model=model,
        system_prompt=NEUTRAL_SYSTEM_PROMPT,
        tools=[],  # pure text, no file, shell or web access
        max_turns=3,  # the structured JSON reply arrives through an internal tool call, which can take a second turn
        setting_sources=[],  # hermetic: ignore the user's own ~/.claude settings and hooks
        permission_mode="default",
        env=environment,
        thinking={"type": "disabled"},
        output_format={"type": "json_schema", "schema": json_schema},
        **connector_options,
    )


def summarise_init_message_data(init_data: dict) -> tuple[int, tuple[str, ...], int]:
    """(number of tools, names of tools starting `mcp__`, number of MCP servers) from a system/init message's data."""
    tool_names = [str(name) for name in (init_data.get("tools") or [])]
    non_built_in_tool_names = tuple(name for name in tool_names if name.startswith("mcp__"))
    return len(tool_names), non_built_in_tool_names, len(init_data.get("mcp_servers") or [])


def call_claude_code_cli_for_structured_reply(
        model: str, user_message: str, json_schema: dict, shut_out_claude_ai_connectors: bool = True) -> ClaudeCodeReply:
    import json
    import anyio
    from claude_agent_sdk import query
    from claude_agent_sdk.types import ResultMessage, SystemMessage

    options = build_judge_options(model, json_schema, shut_out_claude_ai_connectors)

    async def run_query():
        result_message = None
        init_data = None
        stream = query(prompt=user_message, options=options)
        try:
            async for message in stream:
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    init_data = message.data
                elif isinstance(message, ResultMessage):
                    result_message = message
                    break
        finally:
            await stream.aclose()
        return result_message, init_data

    try:
        result_message, init_data = anyio.run(run_query)
    except Exception as error:
        raise _classify(str(error)) from error
    if result_message is None:
        raise ClaudeCodeCallFailed("the CLI returned no result message")
    if result_message.is_error:
        raise _classify(result_message.result or result_message.subtype or "unknown CLI error")
    if result_message.structured_output is not None:
        reply_text = json.dumps(result_message.structured_output, ensure_ascii=False)
    else:
        reply_text = result_message.result or ""  # caller's validation will reject it
    usage = result_message.usage or {}
    number_of_tools_loaded, names_of_non_built_in_tools, number_of_mcp_servers_loaded = (
        summarise_init_message_data(init_data) if init_data is not None else (None, (), None))
    return ClaudeCodeReply(
        reply_text=reply_text,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        reported_cost_usd=result_message.total_cost_usd,
        number_of_tools_loaded=number_of_tools_loaded,
        names_of_non_built_in_tools=names_of_non_built_in_tools,
        number_of_mcp_servers_loaded=number_of_mcp_servers_loaded,
        number_of_turns=result_message.num_turns,
    )


def _classify(message: str) -> Exception:
    lowered = message.lower()
    if any(marker in lowered for marker in USAGE_LIMIT_MARKERS):
        return ClaudeCodeUsageLimitReached(message)
    return ClaudeCodeCallFailed(message)
