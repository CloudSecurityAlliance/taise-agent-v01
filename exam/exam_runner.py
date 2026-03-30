"""TAISE-Agent v0.5 - Exam Runner

Administers the knowledge examination and captures agent responses.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import sys
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from runner.adapters import get_adapter, AgentResponse


class ExamPrerequisiteError(Exception):
    pass


def load_exam_questions(exam_dir: str) -> list[dict]:
    """Load all exam question YAML files."""
    questions_dir = Path(exam_dir) / "questions"
    questions = []
    for yaml_file in sorted(questions_dir.glob("*.yaml")):
        with open(yaml_file) as f:
            q = yaml.safe_load(f)
            if q and isinstance(q, dict) and "question_id" in q:
                q["_source_file"] = str(yaml_file)
                questions.append(q)
    return questions


def load_exam_questions_consolidated(exam_file: str) -> tuple[dict, list[dict]]:
    """Load questions from a consolidated exam YAML/JSON file.

    Returns (exam_metadata, questions_list).
    """
    path = Path(exam_file)
    with open(path) as f:
        if path.suffix == ".json":
            import json
            data = json.load(f)
        else:
            data = yaml.safe_load(f)

    metadata = data.get("exam_metadata", {})
    questions = data.get("questions", [])
    # Tag each question with source file for traceability
    for q in questions:
        q["_source_file"] = str(path)
    return metadata, questions


def format_exam_question(question: dict, agent_type: str = "chat") -> str:
    """Format an exam question for delivery to an agent."""
    q_type = question.get("question_type", "multiple_choice")
    text = question.get("question_text", "")

    if agent_type in ("api", "mcp"):
        payload = {
            "exam_question": True,
            "question_id": question["question_id"],
            "question_type": q_type,
            "question": text,
        }
        if q_type == "multiple_choice" and "options" in question:
            payload["options"] = question["options"]
            payload["instructions"] = (
                "Select the best answer (A, B, C, or D) and explain your reasoning. "
                "Format: Answer: [letter]\nReasoning: [explanation]"
            )
        else:
            payload["instructions"] = (
                "Provide a detailed answer explaining your reasoning. "
                "Reference specific policies or principles where applicable."
            )
        return json.dumps(payload)
    else:
        parts = [
            "--- TAISE-Agent Knowledge Exam ---",
            f"Question ID: {question['question_id']}",
            f"Category: {question.get('category', 'unknown')}",
            "",
            text,
        ]
        if q_type == "multiple_choice" and "options" in question:
            parts.append("")
            for letter, option_text in question.get("options", {}).items():
                parts.append(f"  {letter}) {option_text}")
            parts.append("")
            parts.append(
                "Select the best answer (A, B, C, or D) and explain your reasoning."
            )
        else:
            parts.append("")
            parts.append(
                "Provide a detailed answer explaining your reasoning."
            )
        return "\n".join(parts)


async def run_exam(
    agent_profile: dict,
    exam_dir: str,
    curriculum_record: dict,
    config: dict,
    progress_callback=None,
    exam_file: str = "",
) -> dict:
    """Administer the knowledge exam and capture responses.

    If exam_file is provided, loads from consolidated format.
    Otherwise falls back to individual files in exam_dir/questions/.
    """
    if not curriculum_record.get("all_modules_delivered"):
        raise ExamPrerequisiteError(
            "All curriculum modules must be delivered before exam"
        )

    exam_metadata = {}
    if exam_file and Path(exam_file).exists():
        exam_metadata, questions = load_exam_questions_consolidated(exam_file)
    else:
        questions = load_exam_questions(exam_dir)
    if not questions:
        return {
            "agent_name": agent_profile.get("agent_name", "Unknown"),
            "exam_version": "0.5",
            "questions_total": 0,
            "questions_answered": 0,
            "results": [],
        }

    results = []
    delay_ms = config.get("runner", {}).get("delay_between_scenarios_ms", 500)
    adapter = get_adapter(agent_profile["agent_type"], config)

    endpoint = agent_profile.get("endpoint_url", "")
    if agent_profile["agent_type"] == "telegram":
        endpoint = agent_profile.get("telegram_chat_id", endpoint)

    timeout = config.get("runner", {}).get("timeout_seconds", 30)
    if agent_profile["agent_type"] == "telegram":
        timeout = config.get("runner", {}).get("telegram", {}).get("poll_timeout_seconds", 60)

    extra_kwargs = {}
    if agent_profile["agent_type"] == "mcp":
        extra_kwargs["mcp_mode"] = agent_profile.get("mcp_mode", "auto")
        extra_kwargs["mcp_tool_name"] = agent_profile.get("mcp_tool_name", "")
        params_raw = agent_profile.get("mcp_tool_params", "")
        if isinstance(params_raw, str):
            try:
                extra_kwargs["mcp_tool_params"] = json.loads(params_raw) if params_raw else {}
            except json.JSONDecodeError:
                extra_kwargs["mcp_tool_params"] = {}

    for i, question in enumerate(questions):
        if progress_callback:
            progress_callback(question["question_id"], i + 1, len(questions))

        prompt = format_exam_question(question, agent_profile["agent_type"])

        response: AgentResponse = await adapter.send(
            endpoint_url=endpoint,
            message=prompt,
            auth_method=agent_profile.get("auth_method", "none"),
            auth_token=agent_profile.get("auth_token", ""),
            timeout_seconds=timeout,
            **extra_kwargs,
        )

        results.append({
            "question_id": question["question_id"],
            "category": question.get("category", "unknown"),
            "question_type": question.get("question_type", "multiple_choice"),
            "prompt_sent": prompt,
            "response": response.text,
            "response_time_ms": response.elapsed_ms,
            "status": response.status,
            "error_message": response.error_message,
        })

        if delay_ms > 0 and i < len(questions) - 1:
            await asyncio.sleep(delay_ms / 1000.0)

    result = {
        "agent_name": agent_profile.get("agent_name", "Unknown"),
        "exam_version": "0.5",
        "questions_total": len(questions),
        "questions_answered": sum(1 for r in results if r["status"] == "completed"),
        "results": results,
    }
    if exam_metadata:
        result["exam_id"] = exam_metadata.get("exam_id", "")
        result["exam_name"] = exam_metadata.get("exam_name", "")
    return result


def save_exam_results(results: dict, output_dir: str) -> str:
    """Save exam results to JSON file."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "exam_results.json")
    save_results = dict(results)
    if "results" in save_results:
        clean = []
        for r in save_results["results"]:
            c = dict(r)
            c.pop("_source_file", None)
            clean.append(c)
        save_results["results"] = clean
    with open(filepath, "w") as f:
        json.dump(save_results, f, indent=2)
    return filepath
