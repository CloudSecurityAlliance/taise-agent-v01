"""
TAISE-Agent v0.5 - Multi-Turn Scenario Runner

Executes multi-turn scenarios maintaining conversation context between turns.
For agents that don't support multi-turn, concatenates all turns into a single message.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from .adapters import AgentResponse, get_adapter


async def run_multi_turn_scenario(
    agent_profile: dict,
    scenario: dict,
    config: dict,
) -> dict:
    """Execute a multi-turn scenario maintaining conversation context.

    For agents with multi_turn_capable=true, sends each turn sequentially
    within the same conversation context.

    For agents without multi-turn support, concatenates all turns into
    a single message with turn markers.
    """
    multi_turn_capable = agent_profile.get("multi_turn_capable", False)
    turns = scenario.get("turns", [])

    if not turns:
        return {
            "scenario_id": scenario["scenario_id"],
            "category": scenario["category"],
            "multi_turn": True,
            "turns": [],
            "status": "error",
            "error_message": "No turns defined in multi-turn scenario",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if multi_turn_capable:
        return await _run_multi_turn_native(agent_profile, scenario, config)
    else:
        return await _run_multi_turn_concatenated(agent_profile, scenario, config)


async def _run_multi_turn_native(
    agent_profile: dict,
    scenario: dict,
    config: dict,
) -> dict:
    """Run multi-turn scenario with native conversation support."""
    adapter = get_adapter(agent_profile["agent_type"], config)
    turns = scenario["turns"]
    turn_results = []

    timeout = config.get("runner", {}).get("timeout_seconds", 30)
    delay_ms = config.get("runner", {}).get("delay_between_scenarios_ms", 500)

    endpoint = agent_profile.get("endpoint_url", "")
    if agent_profile["agent_type"] == "telegram":
        endpoint = agent_profile.get("telegram_chat_id", endpoint)

    # Build conversation history for context
    conversation_history = []

    for turn_spec in turns:
        prompt = turn_spec["prompt"]

        # For API-type agents, send full conversation history
        if agent_profile["agent_type"] == "api":
            conversation_history.append({"role": "user", "content": prompt})
            # The API adapter sends messages array; we'd need to modify it
            # For now, just send the current turn prompt

        response: AgentResponse = await adapter.send(
            endpoint_url=endpoint,
            message=prompt,
            auth_method=agent_profile.get("auth_method", "none"),
            auth_token=agent_profile.get("auth_token", ""),
            timeout_seconds=timeout,
        )

        turn_results.append({
            "turn": turn_spec["turn"],
            "prompt": prompt,
            "response": response.text,
            "expected": turn_spec.get("expected", "within_scope"),
            "response_time_ms": response.elapsed_ms,
            "status": response.status,
        })

        if response.text:
            conversation_history.append({"role": "assistant", "content": response.text})

        # Short delay between turns
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    return {
        "scenario_id": scenario["scenario_id"],
        "category": scenario["category"],
        "subcategory": scenario.get("subcategory", ""),
        "name": scenario.get("name", ""),
        "severity": scenario.get("severity", "medium"),
        "multi_turn": True,
        "turns": turn_results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if all(t["status"] == "completed" for t in turn_results) else "partial",
        "outcome_type": "scored_behavior",
    }


async def _run_multi_turn_concatenated(
    agent_profile: dict,
    scenario: dict,
    config: dict,
) -> dict:
    """Run multi-turn scenario by concatenating turns into a single message."""
    adapter = get_adapter(agent_profile["agent_type"], config)
    turns = scenario["turns"]

    # Build concatenated prompt
    parts = [
        "The following is a multi-turn conversation scenario. "
        "Please respond to each turn in sequence, indicating your response for each.\n"
    ]
    for turn_spec in turns:
        parts.append(f"\n--- Turn {turn_spec['turn']} ---")
        parts.append(f"User: {turn_spec['prompt']}")
        parts.append("Your response:")

    concatenated_prompt = "\n".join(parts)

    timeout = config.get("runner", {}).get("timeout_seconds", 30)
    endpoint = agent_profile.get("endpoint_url", "")
    if agent_profile["agent_type"] == "telegram":
        endpoint = agent_profile.get("telegram_chat_id", endpoint)

    response: AgentResponse = await adapter.send(
        endpoint_url=endpoint,
        message=concatenated_prompt,
        auth_method=agent_profile.get("auth_method", "none"),
        auth_token=agent_profile.get("auth_token", ""),
        timeout_seconds=timeout,
    )

    # Create turn results from the single concatenated response
    turn_results = []
    for turn_spec in turns:
        turn_results.append({
            "turn": turn_spec["turn"],
            "prompt": turn_spec["prompt"],
            "response": response.text,  # Full response for all turns
            "expected": turn_spec.get("expected", "within_scope"),
            "response_time_ms": response.elapsed_ms // len(turns),
            "status": response.status,
        })

    return {
        "scenario_id": scenario["scenario_id"],
        "category": scenario["category"],
        "subcategory": scenario.get("subcategory", ""),
        "name": scenario.get("name", ""),
        "severity": scenario.get("severity", "medium"),
        "multi_turn": True,
        "concatenated": True,
        "prompt": concatenated_prompt,
        "response": response.text,
        "turns": turn_results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": response.status,
        "outcome_type": "scored_behavior",
        "response_time_ms": response.elapsed_ms,
    }
