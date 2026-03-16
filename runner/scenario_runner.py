"""
TAISE-Agent v0.3 - Scenario Runner

Loads scenario files, sends test prompts to agent endpoints,
captures responses, and produces a structured transcript.

v0.3: IAP-based scenario filtering and outcome classification.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .adapters import AgentResponse, get_adapter
from .outcome_classifier import (
    classify_outcome,
    get_default_iap,
    scenario_matches_profile,
)
from .tool_boundary_runner import ToolBoundaryRunner, load_tool_boundary_scenarios


def load_scenarios(scenario_dir: str) -> list[dict]:
    """Load all YAML scenario files from the scenario directory tree."""
    scenarios = []
    scenario_path = Path(scenario_dir)

    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario directory not found: {scenario_dir}")

    for yaml_file in sorted(scenario_path.rglob("*.yaml")):
        with open(yaml_file, "r") as f:
            scenario = yaml.safe_load(f)
            scenario["_source_file"] = str(yaml_file)
            scenarios.append(scenario)

    if not scenarios:
        raise ValueError(f"No YAML scenario files found in: {scenario_dir}")

    return scenarios


async def run_single_scenario(
    scenario: dict,
    agent_profile: dict,
    config: dict,
) -> dict:
    """Run a single scenario against an agent and return the transcript entry."""
    adapter = get_adapter(agent_profile["agent_type"], config)
    # Use type-specific timeout if available
    agent_type = agent_profile["agent_type"]
    if agent_type in ("mcp",) and config.get("runner", {}).get("mcp", {}).get("timeout_seconds"):
        timeout = config["runner"]["mcp"]["timeout_seconds"]
    elif agent_type in ("telegram",) and config.get("runner", {}).get("telegram", {}).get("poll_timeout_seconds"):
        timeout = config["runner"]["telegram"]["poll_timeout_seconds"]
    else:
        timeout = config.get("runner", {}).get("timeout_seconds", 30)

    # For Telegram bots, use the chat_id as the endpoint identifier
    if agent_profile["agent_type"] == "telegram":
        endpoint = agent_profile.get("telegram_chat_id", agent_profile["endpoint_url"])
    else:
        endpoint = agent_profile["endpoint_url"]

    # Build extra kwargs for MCP agents
    extra_kwargs = {}
    if agent_profile["agent_type"] == "mcp":
        extra_kwargs["mcp_mode"] = agent_profile.get("mcp_mode", "auto")
        extra_kwargs["mcp_tool_name"] = agent_profile.get("mcp_tool_name", "")
        # Pass through any static tool params
        params_raw = agent_profile.get("mcp_tool_params", "")
        if isinstance(params_raw, str):
            try:
                extra_kwargs["mcp_tool_params"] = json.loads(params_raw) if params_raw else {}
            except json.JSONDecodeError:
                extra_kwargs["mcp_tool_params"] = {}
        elif isinstance(params_raw, dict):
            extra_kwargs["mcp_tool_params"] = params_raw

    # Handle both single-turn ("prompt") and multi-turn ("turns") scenarios
    if scenario.get("turns"):
        # Multi-turn: send each user turn, collect responses
        turns = scenario["turns"]
        user_turns = [t for t in turns if t.get("role") == "user"]
        all_responses = []
        last_response = None

        for turn in user_turns:
            last_response = await adapter.send(
                endpoint_url=endpoint,
                message=turn["content"],
                auth_method=agent_profile.get("auth_method", "none"),
                auth_token=agent_profile.get("auth_token", ""),
                timeout_seconds=timeout,
                **extra_kwargs,
            )
            all_responses.append(last_response)
            if last_response.status != "completed":
                break
            # Brief delay between turns
            await asyncio.sleep(1.0)

        # Combine for scoring: use last response as primary, join all for context
        combined_text = "\n---\n".join(r.text for r in all_responses if r.text)
        total_ms = sum(r.elapsed_ms for r in all_responses)
        prompt_text = "\n".join(t["content"] for t in user_turns)
        final_status = last_response.status if last_response else "connection_error"
        final_error = last_response.error_message if last_response else "No turns executed"

        response = AgentResponse(
            text=combined_text,
            elapsed_ms=total_ms,
            status=final_status,
            raw_response=None,
            error_message=final_error,
        )
    else:
        prompt_text = scenario["prompt"]
        response = await adapter.send(
            endpoint_url=endpoint,
            message=prompt_text,
            auth_method=agent_profile.get("auth_method", "none"),
            auth_token=agent_profile.get("auth_token", ""),
            timeout_seconds=timeout,
            **extra_kwargs,
        )

    entry = {
        "scenario_id": scenario["scenario_id"],
        "category": scenario["category"],
        "subcategory": scenario.get("subcategory", ""),
        "name": scenario.get("name", ""),
        "severity": scenario.get("severity", "medium"),
        "prompt": prompt_text,
        "response": response.text,
        "response_time_ms": response.elapsed_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": response.status,
        "error_message": response.error_message,
    }

    # Include raw_response for MCP agents (contains tool_calls)
    if response.raw_response and agent_profile.get("agent_type") in ("mcp",):
        entry["raw_response"] = response.raw_response

    # v0.3: Classify outcome type
    iap = agent_profile.get("iap")
    if iap is None:
        # No IAP — all completed scenarios are scored_behavior (backward compat)
        entry["outcome_type"] = "scored_behavior" if entry["status"] == "completed" else "infrastructure_failure"
    else:
        entry["outcome_type"] = classify_outcome(entry, scenario, iap)

    return entry


async def run_scenarios(
    agent_profile: dict,
    scenario_dir: str,
    config: dict,
    progress_callback=None,
) -> dict:
    """Run all scenarios against an agent and produce the full transcript.

    Args:
        agent_profile: The agent profile dict (from agent_profile.json)
        scenario_dir: Path to the scenarios directory
        config: Configuration dict (from config.yaml)
        progress_callback: Optional callback(scenario_id, index, total) for progress updates

    Returns:
        Complete transcript dict ready to be written to test_transcript.json
    """
    all_scenarios = load_scenarios(scenario_dir)
    transcript = []
    connection_failures = 0
    max_failures = config.get("runner", {}).get("max_connection_failures", 5)
    delay_ms = config.get("runner", {}).get("delay_between_scenarios_ms", 500)

    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # v0.3: Filter scenarios by IAP applicability
    # If no IAP is provided, all scenarios are applicable (backward compatibility)
    iap = agent_profile.get("iap")
    applicable_scenarios = []
    tool_boundary_scenarios = []
    skipped_entries = []

    if iap is None:
        applicable_scenarios = [s for s in all_scenarios if s.get("evaluation_mode") != "tool_boundary"]
        tool_boundary_scenarios = [s for s in all_scenarios if s.get("evaluation_mode") == "tool_boundary"]
    else:
        for scenario in all_scenarios:
            if scenario.get("evaluation_mode") == "tool_boundary":
                # Tool boundary scenarios go to the ToolBoundaryRunner
                # They're applicable if agent is MCP type
                if iap.get("interface_type") == "mcp_server":
                    tool_boundary_scenarios.append(scenario)
                else:
                    skipped_entries.append({
                        "scenario_id": scenario["scenario_id"],
                        "category": scenario["category"],
                        "subcategory": scenario.get("subcategory", ""),
                        "name": scenario.get("name", ""),
                        "severity": scenario.get("severity", "medium"),
                        "prompt": f"[tool_boundary] {scenario.get('name', '')}",
                        "response": "",
                        "response_time_ms": 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "not_applicable",
                        "error_message": None,
                        "outcome_type": "not_applicable",
                    })
            elif scenario_matches_profile(scenario, iap):
                applicable_scenarios.append(scenario)
            else:
                skipped_entries.append({
                    "scenario_id": scenario["scenario_id"],
                    "category": scenario["category"],
                    "subcategory": scenario.get("subcategory", ""),
                    "name": scenario.get("name", ""),
                    "severity": scenario.get("severity", "medium"),
                    "prompt": scenario.get("prompt", f"[{scenario['scenario_id']}]"),
                    "response": "",
                    "response_time_ms": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "not_applicable",
                    "error_message": None,
                    "outcome_type": "not_applicable",
                })

    total_to_run = len(applicable_scenarios)
    for i, scenario in enumerate(applicable_scenarios):
        if progress_callback:
            progress_callback(scenario["scenario_id"], i + 1, total_to_run)

        entry = await run_single_scenario(scenario, agent_profile, config)
        transcript.append(entry)

        # Track connection failures
        if entry["status"] == "connection_error":
            connection_failures += 1
            if connection_failures > max_failures:
                # Abort run - endpoint is not functional enough
                break

        # Rate limiting delay between scenarios
        if delay_ms > 0 and i < total_to_run - 1:
            await asyncio.sleep(delay_ms / 1000.0)

    # v0.4: Run tool boundary scenarios via ToolBoundaryRunner
    if tool_boundary_scenarios and agent_profile.get("agent_type") == "mcp":
        try:
            mcp_adapter = get_adapter("mcp", config)
            tb_runner = ToolBoundaryRunner(mcp_adapter, agent_profile, tool_boundary_scenarios, config)
            tb_result = await tb_runner.run_all(progress_callback=progress_callback)
            transcript.extend(tb_result.get("transcript", []))
        except Exception as e:
            # If tool boundary runner fails entirely, mark all as infra failures
            for scenario in tool_boundary_scenarios:
                transcript.append({
                    "scenario_id": scenario["scenario_id"],
                    "category": scenario.get("category", "unknown"),
                    "subcategory": scenario.get("subcategory", ""),
                    "name": scenario.get("name", ""),
                    "severity": scenario.get("severity", "medium"),
                    "prompt": f"[tool_boundary] Runner failed: {e}",
                    "response": "",
                    "response_time_ms": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "connection_error",
                    "error_message": f"{type(e).__name__}: {str(e)}",
                    "outcome_type": "infrastructure_failure",
                    "evaluation_mode": "tool_boundary",
                })

    # Append skipped (not_applicable) entries at end
    transcript.extend(skipped_entries)

    completed = sum(1 for t in transcript if t["status"] == "completed")
    failed = sum(1 for t in transcript if t["status"] not in ("completed", "not_applicable"))

    return {
        "agent_name": agent_profile["agent_name"],
        "run_id": run_id,
        "scenarios_total": len(all_scenarios),
        "scenarios_applicable": len(applicable_scenarios) + len(tool_boundary_scenarios),
        "scenarios_completed": completed,
        "scenarios_failed": failed,
        "scenarios_not_applicable": len(skipped_entries),
        "aborted": connection_failures > max_failures,
        "started_at": transcript[0]["timestamp"] if transcript else None,
        "completed_at": transcript[-1]["timestamp"] if transcript else None,
        "transcript": transcript,
    }


def save_transcript(transcript: dict, output_dir: str) -> str:
    """Save the transcript to a JSON file and return the file path."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "test_transcript.json")
    with open(filepath, "w") as f:
        json.dump(transcript, f, indent=2)
    return filepath
