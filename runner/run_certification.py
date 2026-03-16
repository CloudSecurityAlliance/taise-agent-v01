#!/usr/bin/env python3
"""
TAISE-Agent v0.4 — Main Pipeline Orchestrator

Runs the complete certification pipeline:
  Agent Profile → Scenario Runner → Evaluation Engine → Scoring Engine → Report Generator

v0.4: MCP capability boundary testing.
v0.3: IAP-based scenario filtering and outcome-aware scoring.

Usage:
    python run_certification.py --agent agents/agent_profile.json
    python run_certification.py --agent agents/agent_profile.json --scenarios scenarios/ --config config.yaml
    python run_certification.py --agent agents/agent_profile.json --skip-judge  # Rule-based only

The pipeline is linear and fail-fast: if any component fails, the pipeline stops.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from runner.scenario_runner import run_scenarios, save_transcript
from runner.tool_boundary_runner import ToolBoundaryRunner, load_tool_boundary_scenarios
from evaluation.reconciler import evaluate_transcript, save_evaluation_results
from evaluation.rule_engine import load_scenario_metadata, rule_evaluate
from scoring.scoring_engine import compute_certification_score, save_certification_score
from reports.report_generator import generate_report, save_report


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_agent_profile(profile_path: str) -> dict:
    """Load agent profile from JSON file."""
    with open(profile_path, "r") as f:
        return json.load(f)


def create_run_directory(agent_name: str, base_dir: str = "runs") -> str:
    """Create a timestamped run directory for artifacts."""
    safe_name = agent_name.replace(" ", "_").lower()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(base_dir, f"{safe_name}_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def print_progress(scenario_id: str, index: int, total: int):
    """Print progress to stdout."""
    bar_width = 30
    filled = int(bar_width * index / total)
    bar = "█" * filled + "░" * (bar_width - filled)
    print(f"\r  [{bar}] {index}/{total} — {scenario_id}", end="", flush=True)
    if index == total:
        print()  # Newline after completion


async def run_rule_only_evaluation(
    transcript: dict,
    scenario_dir: str,
    config: dict,
    progress_callback=None,
) -> dict:
    """Run evaluation using only rule-based engine (no AI judge).

    Used when --skip-judge flag is set or when API keys are unavailable.
    """
    from evaluation.rule_engine import load_scenario_metadata, rule_evaluate
    from datetime import datetime, timezone

    scenarios = load_scenario_metadata(scenario_dir)
    evaluations = []

    entries = transcript.get("transcript", [])
    for i, entry in enumerate(entries):
        scenario_id = entry["scenario_id"]
        scenario = scenarios.get(scenario_id)

        if progress_callback:
            progress_callback(scenario_id, i + 1, len(entries))

        # v0.3: Pass through non-scored_behavior outcomes without evaluation
        outcome_type = entry.get("outcome_type", "scored_behavior")
        if outcome_type != "scored_behavior":
            evaluations.append({
                "scenario_id": scenario_id,
                "category": entry.get("category", "unknown"),
                "outcome_type": outcome_type,
                "final_score": 0,
                "max_score": 3,
                "evaluation_agreement": None,
                "flagged_for_review": False,
                "reconciliation_note": f"Not evaluated: {outcome_type}",
            })
            continue

        # v0.4: Handle tool boundary scenarios (pre-evaluated)
        if entry.get("evaluation_mode") == "tool_boundary":
            tb_eval = entry.get("tool_boundary_evaluation", {})
            tb_score = tb_eval.get("final_score", 0)
            evaluations.append({
                "scenario_id": scenario_id,
                "category": entry.get("category", "unknown"),
                "subcategory": entry.get("subcategory", ""),
                "name": entry.get("name", ""),
                "severity": entry.get("severity", "medium"),
                "scoring_weight": tb_eval.get("scoring_weight", 1.0),
                "outcome_type": "scored_behavior",
                "evaluation_mode": "tool_boundary",
                "rule_based": {"rule_score": tb_score, "tool_boundary": True},
                "ai_judge": {"score": -1, "reasoning": "Tool boundary (rule-only mode)", "disqualifying_signal_detected": False, "confidence": 0.0},
                "final_score": tb_score,
                "max_score": 3,
                "evaluation_agreement": None,
                "flagged_for_review": tb_eval.get("disqualified", False),
                "reconciliation_note": f"Tool boundary: {tb_eval.get('attacks_passed', 0)}/{tb_eval.get('attack_count', 0)} passed",
            })
            continue

        if not scenario:
            evaluations.append({
                "scenario_id": scenario_id,
                "category": entry.get("category", "unknown"),
                "outcome_type": "scored_behavior",
                "error": f"Scenario metadata not found for {scenario_id}",
                "final_score": 0,
                "max_score": 3,
            })
            continue

        if entry.get("status") != "completed":
            evaluations.append({
                "scenario_id": scenario_id,
                "category": entry.get("category", "unknown"),
                "outcome_type": outcome_type,
                "rule_based": {"rule_score": 0, "disqualified": True},
                "ai_judge": {"score": -1, "reasoning": "AI judge skipped", "disqualifying_signal_detected": False, "confidence": 0.0},
                "final_score": 0,
                "max_score": 3,
                "evaluation_agreement": None,
                "flagged_for_review": True,
                "reconciliation_note": f"Scenario did not complete: {entry.get('status')}",
            })
            continue

        rule_result = rule_evaluate(scenario, entry["response"])

        evaluations.append({
            "scenario_id": scenario_id,
            "category": entry.get("category", "unknown"),
            "subcategory": entry.get("subcategory", ""),
            "name": entry.get("name", ""),
            "severity": entry.get("severity", "medium"),
            "scoring_weight": scenario.get("scoring_weight", 1.0),
            "outcome_type": "scored_behavior",
            "rule_based": rule_result,
            "ai_judge": {"score": -1, "reasoning": "AI judge skipped (--skip-judge)", "disqualifying_signal_detected": False, "confidence": 0.0},
            "final_score": rule_result["rule_score"],
            "max_score": 3,
            "evaluation_agreement": None,
            "flagged_for_review": False,
            "reconciliation_note": "Rule-based evaluation only (AI judge skipped)",
        })

    return {
        "run_id": transcript.get("run_id", "unknown"),
        "agent_name": transcript.get("agent_name", "unknown"),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "evaluations": evaluations,
    }


async def run_pipeline(
    agent_profile_path: str,
    scenario_dir: str,
    config_path: str,
    skip_judge: bool = False,
    verbose: bool = True,
) -> dict:
    """Run the complete TAISE-Agent v0.1 certification pipeline.

    Returns:
        Dict with all artifacts and the run directory path.
    """
    # Load configuration
    config = load_config(config_path)
    agent_profile = load_agent_profile(agent_profile_path)

    agent_name = agent_profile.get("agent_name", "unknown")
    if verbose:
        print(f"\n{'='*60}")
        print(f"  TAISE-Agent v0.4 Certification Pipeline")
        print(f"  Cloud Security Alliance AI Safety Initiative")
        print(f"{'='*60}")
        print(f"\n  Agent: {agent_name}")
        print(f"  Endpoint: {agent_profile.get('endpoint_url', 'N/A')}")
        print(f"  Type: {agent_profile.get('agent_type', 'N/A')}")
        iap = agent_profile.get("iap", {})
        if iap:
            print(f"  IAP: {iap.get('interface_type', 'default')}/{iap.get('capability_posture', 'default')}/L{iap.get('autonomy_level', '?')}")
        print(f"  Judge: {'Skipped' if skip_judge else config.get('judge', {}).get('provider', 'anthropic')}")
        print()

    # Create run directory
    run_dir = create_run_directory(agent_name)

    # Save agent profile to run directory
    profile_path = os.path.join(run_dir, "agent_profile.json")
    with open(profile_path, "w") as f:
        json.dump(agent_profile, f, indent=2)

    # ── Step 1: Run Scenarios ──
    if verbose:
        print("Step 1/4: Running scenarios against agent...")
    transcript = await run_scenarios(
        agent_profile=agent_profile,
        scenario_dir=scenario_dir,
        config=config,
        progress_callback=print_progress if verbose else None,
    )
    transcript_path = save_transcript(transcript, run_dir)

    if transcript.get("aborted"):
        print(f"\n  ⚠ Run aborted: Too many connection failures")
        print(f"  Completed {transcript['scenarios_completed']}/{transcript['scenarios_total']} scenarios")

    if verbose:
        completed = transcript["scenarios_completed"]
        total = transcript["scenarios_total"]
        applicable = transcript.get("scenarios_applicable", total)
        na = transcript.get("scenarios_not_applicable", 0)
        print(f"  ✓ {completed}/{applicable} applicable scenarios completed ({na} skipped by IAP)")
        print()

    # ── Step 1b: Run Tool Boundary Scenarios (v0.4, MCP servers only) ──
    iap = agent_profile.get("iap", {})
    if iap.get("interface_type") == "mcp_server":
        tb_scenarios = load_tool_boundary_scenarios(scenario_dir)
        if tb_scenarios:
            if verbose:
                print("Step 1b/4: Running tool boundary scenarios against MCP server...")
            from runner.mcp_adapter import MCPAdapter
            mcp_adapter = MCPAdapter(config)
            tb_runner = ToolBoundaryRunner(mcp_adapter, agent_profile, tb_scenarios, config)
            tb_result = await tb_runner.run_all(
                progress_callback=print_progress if verbose else None,
            )
            # Merge tool boundary transcript entries with behavioral entries
            tb_entries = tb_result.get("transcript", [])
            transcript["transcript"].extend(tb_entries)
            tb_completed = sum(1 for t in tb_entries if t["status"] == "completed")
            tb_total = len(tb_entries)
            transcript["scenarios_total"] += tb_total
            transcript["scenarios_applicable"] = transcript.get("scenarios_applicable", 0) + tb_total
            transcript["scenarios_completed"] += tb_completed
            if verbose:
                print(f"  ✓ {tb_completed}/{tb_total} tool boundary scenarios completed")
                print(f"    ({tb_result.get('tools_discovered', 0)} tools discovered)")
                print()

    # ── Step 2: Evaluate Responses ──
    if verbose:
        print("Step 2/4: Evaluating agent responses...")

    if skip_judge:
        evaluation_results = await run_rule_only_evaluation(
            transcript, scenario_dir, config,
            progress_callback=print_progress if verbose else None,
        )
    else:
        evaluation_results = await evaluate_transcript(
            transcript, scenario_dir, config,
            progress_callback=print_progress if verbose else None,
        )

    eval_path = save_evaluation_results(evaluation_results, run_dir)

    if verbose:
        evals = evaluation_results["evaluations"]
        passed = sum(1 for e in evals if e.get("final_score", 0) >= 2)
        flagged = sum(1 for e in evals if e.get("flagged_for_review"))
        print(f"  ✓ {len(evals)} scenarios evaluated, {passed} passed, {flagged} flagged")
        print()

    # ── Step 3: Compute Certification Score ──
    if verbose:
        print("Step 3/4: Computing certification score...")

    certification_score = compute_certification_score(evaluation_results, config)
    score_path = save_certification_score(certification_score, run_dir)

    if verbose:
        overall = certification_score["overall_score"]
        decision = certification_score["decision"]
        icon = "✓" if decision == "PASS" else ("⚠" if decision == "CONDITIONAL" else "✗")
        print(f"  {icon} Overall Score: {overall}/100 — {decision}")

        for cat_name, cat_data in certification_score["category_scores"].items():
            cat_icon = "✓" if cat_data["score"] >= 80 else ("⚠" if cat_data["score"] >= 60 else "✗")
            print(f"    {cat_icon} {cat_name}: {cat_data['score']}/100")
        print()

    # ── Step 4: Generate Report ──
    if verbose:
        print("Step 4/4: Generating certification report...")

    report_md = generate_report(
        agent_profile=agent_profile,
        certification_score=certification_score,
        evaluation_results=evaluation_results,
        transcript=transcript,
        config=config,
    )
    report_path = save_report(report_md, run_dir)

    if verbose:
        print(f"  ✓ Report generated: {report_path}")
        print()
        print(f"{'='*60}")
        print(f"  Certification Complete!")
        print(f"  Run directory: {run_dir}")
        print(f"{'='*60}")
        print(f"\n  Artifacts:")
        print(f"    • Agent Profile:      {profile_path}")
        print(f"    • Test Transcript:    {transcript_path}")
        print(f"    • Evaluation Results: {eval_path}")
        print(f"    • Certification Score: {score_path}")
        print(f"    • Report:             {report_path}")
        print()

    return {
        "run_dir": run_dir,
        "agent_profile": agent_profile,
        "transcript": transcript,
        "evaluation_results": evaluation_results,
        "certification_score": certification_score,
        "report_path": report_path,
    }


def main():
    parser = argparse.ArgumentParser(
        description="TAISE-Agent v0.1 Certification Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_certification.py --agent agents/my_agent.json
  python run_certification.py --agent agents/my_agent.json --skip-judge
  python run_certification.py --agent agents/my_agent.json --config config.yaml --scenarios scenarios/
        """,
    )
    parser.add_argument(
        "--agent", required=True,
        help="Path to agent profile JSON file",
    )
    parser.add_argument(
        "--scenarios", default="scenarios",
        help="Path to scenarios directory (default: scenarios/)",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to configuration YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--skip-judge", action="store_true",
        help="Skip AI judge evaluation (use rule-based only)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress output",
    )

    args = parser.parse_args()

    # Validate paths
    if not os.path.exists(args.agent):
        print(f"Error: Agent profile not found: {args.agent}")
        sys.exit(1)
    if not os.path.exists(args.scenarios):
        print(f"Error: Scenarios directory not found: {args.scenarios}")
        sys.exit(1)
    if not os.path.exists(args.config):
        print(f"Error: Config file not found: {args.config}")
        sys.exit(1)

    # Run the pipeline
    result = asyncio.run(run_pipeline(
        agent_profile_path=args.agent,
        scenario_dir=args.scenarios,
        config_path=args.config,
        skip_judge=args.skip_judge,
        verbose=not args.quiet,
    ))

    # Exit with appropriate code
    decision = result["certification_score"]["decision"]
    sys.exit(0 if decision == "PASS" else 1)


if __name__ == "__main__":
    main()
