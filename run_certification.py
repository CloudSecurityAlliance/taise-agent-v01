#!/usr/bin/env python3
"""
TAISE-Agent v0.5 — Main Pipeline Orchestrator

Runs the complete certification pipeline with three assessment paths:
  1. Full Certification: Curriculum → Exam → Behavioral Testing → Report
  2. Education & Exam: Curriculum → Exam → Report
  3. Adversarial Only: Behavioral Testing → Report (v0.4 compatible)

v0.5: Education curriculum, knowledge exam, composite scoring, diagnostic matrix.
v0.4: MCP capability boundary testing.
v0.3: IAP-based scenario filtering and outcome-aware scoring.

Usage:
    python run_certification.py --agent agents/agent_profile.json
    python run_certification.py --agent agents/agent_profile.json --path full_certification
    python run_certification.py --agent agents/agent_profile.json --path education_exam
    python run_certification.py --agent agents/agent_profile.json --path adversarial_only
    python run_certification.py --agent agents/agent_profile.json --skip-judge
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
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
    """Create a uniquely-named run directory for artifacts."""
    safe_name = agent_name.replace(" ", "_").lower()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    run_dir = os.path.join(base_dir, f"{safe_name}_{timestamp}_{uid}")
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
    """Run evaluation using only rule-based engine (no AI judge)."""
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


async def run_curriculum_stage(
    agent_profile: dict,
    config: dict,
    run_dir: str,
    verbose: bool = True,
) -> dict:
    """Run the curriculum delivery stage."""
    from curriculum.curriculum_engine import CurriculumEngine

    if verbose:
        print("  Delivering safety curriculum...")

    curriculum_dir = str(PROJECT_ROOT / "curriculum")
    engine = CurriculumEngine(curriculum_dir)
    curriculum_record = engine.deliver_curriculum(agent_profile)
    engine.save_curriculum_record(curriculum_record, run_dir)

    if verbose:
        delivered = len(curriculum_record.get("modules_delivered", []))
        policies = curriculum_record.get("total_policies_covered", 0)
        print(f"  ✓ {delivered}/6 modules delivered ({policies} policies covered)")
        print()

    return curriculum_record


async def run_exam_stage(
    agent_profile: dict,
    curriculum_record: dict,
    config: dict,
    run_dir: str,
    skip_judge: bool = False,
    verbose: bool = True,
    progress_tracker=None,
) -> dict:
    """Run the knowledge examination stage.

    Resilient: if exam delivery or evaluation partially fails, saves and
    returns whatever results were collected so the pipeline can continue.
    """
    import traceback
    from exam.exam_runner import run_exam, save_exam_results
    from evaluation.exam_evaluator import evaluate_exam

    exam_dir = str(PROJECT_ROOT / "exam")
    raw_results = None
    evaluated_results = None

    # ── Deliver exam questions ──
    def exam_progress_cb(q_id, index, total):
        if verbose:
            print_progress(q_id, index, total)
        if progress_tracker:
            progress_tracker("exam", index, total)

    try:
        if verbose:
            print("  Administering knowledge examination...")

        raw_results = await run_exam(
            agent_profile=agent_profile,
            exam_dir=exam_dir,
            curriculum_record=curriculum_record,
            config=config,
            progress_callback=exam_progress_cb,
        )

        if verbose:
            answered = raw_results.get("questions_answered", 0)
            total = raw_results.get("questions_total", 0)
            print(f"  ✓ {answered}/{total} questions answered")
            print()
    except Exception as e:
        print(f"  [ERROR] Exam delivery failed: {e}")
        traceback.print_exc()
        if raw_results is None:
            raw_results = {
                "agent_name": agent_profile.get("agent_name", "Unknown"),
                "exam_version": "0.5",
                "questions_total": 0,
                "questions_answered": 0,
                "results": [],
            }

    # ── Evaluate exam responses ──
    try:
        if verbose:
            print("  Evaluating exam responses...")

        evaluated_results = await evaluate_exam(
            exam_results=raw_results,
            exam_dir=exam_dir,
            config=config,
            skip_judge=skip_judge,
            progress_callback=print_progress if verbose else None,
        )
    except Exception as e:
        print(f"  [ERROR] Exam evaluation failed: {e}")
        traceback.print_exc()
        # Build minimal evaluated results from raw results
        evaluated_results = {
            "agent_name": raw_results.get("agent_name", "Unknown"),
            "exam_version": "0.5",
            "evaluated_at": None,
            "questions_total": raw_results.get("questions_total", 0),
            "questions_answered": raw_results.get("questions_answered", 0),
            "overall_score_pct": 0.0,
            "category_scores": {},
            "pass_threshold_met": False,
            "all_categories_above_minimum": False,
            "results": raw_results.get("results", []),
            "evaluation_error": str(e),
        }

    save_exam_results(evaluated_results, run_dir)

    if verbose:
        score_pct = evaluated_results.get("overall_score_pct", 0)
        passed = evaluated_results.get("pass_threshold_met", False)
        icon = "✓" if passed else "✗"
        print(f"  {icon} Exam Score: {score_pct}% ({'PASS' if passed else 'FAIL'})")
        for cat, data in evaluated_results.get("category_scores", {}).items():
            cat_icon = "✓" if data["pct"] >= 70 else ("⚠" if data["pct"] >= 50 else "✗")
            print(f"    {cat_icon} {cat}: {data['pct']}%")
        print()

    return evaluated_results


async def run_pipeline(
    agent_profile_path: str,
    scenario_dir: str = "scenarios",
    config_path: str = "config.yaml",
    skip_judge: bool = False,
    verbose: bool = True,
    assessment_path: str = "full_certification",
    progress_tracker=None,
) -> dict:
    """Run the TAISE-Agent v0.5 certification pipeline.

    Args:
        agent_profile_path: Path to agent profile JSON
        scenario_dir: Path to scenarios directory
        config_path: Path to config YAML
        skip_judge: Skip AI judge evaluation
        verbose: Print progress
        assessment_path: One of "full_certification", "education_exam", "adversarial_only"
        progress_tracker: Optional callback(phase, current, total) for live progress updates

    Returns:
        Dict with all artifacts and the run directory path.
    """
    # Load configuration
    config = load_config(config_path)
    agent_profile = load_agent_profile(agent_profile_path)

    agent_name = agent_profile.get("agent_name", "unknown")
    # Override assessment path from profile if set
    assessment_path = agent_profile.get("assessment_path", assessment_path)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  TAISE-Agent v0.5 Certification Pipeline")
        print(f"  Cloud Security Alliance AI Safety Initiative")
        print(f"{'='*60}")
        print(f"\n  Agent: {agent_name}")
        print(f"  Endpoint: {agent_profile.get('endpoint_url', 'N/A')}")
        print(f"  Type: {agent_profile.get('agent_type', 'N/A')}")
        print(f"  Assessment Path: {assessment_path.replace('_', ' ').title()}")
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

    # Track step numbers dynamically
    step = 1
    include_curriculum = assessment_path in ("full_certification", "education_exam")
    include_exam = assessment_path in ("full_certification", "education_exam")
    include_behavioral = assessment_path in ("full_certification", "adversarial_only")

    total_steps = sum([include_curriculum, include_exam, include_behavioral, True, True])
    # Steps: curriculum, exam, behavioral, evaluation/scoring, report

    curriculum_record = None
    exam_results = None
    transcript = None
    evaluation_results = None

    # Helper to report progress
    def _track(phase, current=None, total=None):
        if progress_tracker:
            progress_tracker(phase, current, total)

    # ── Curriculum Delivery ──
    if include_curriculum:
        _track("curriculum")
        if verbose:
            print(f"Step {step}/{total_steps}: Delivering safety curriculum...")
        curriculum_record = await run_curriculum_stage(
            agent_profile, config, run_dir, verbose
        )
        step += 1

    # ── Knowledge Examination ──
    if include_exam:
        _track("exam", 0)
        if verbose:
            print(f"Step {step}/{total_steps}: Running knowledge examination...")

        if curriculum_record is None:
            # Create a minimal curriculum record for exam-only path
            curriculum_record = {
                "all_modules_delivered": True,
                "modules_delivered": [],
                "curriculum_version": "0.5",
            }

        exam_results = await run_exam_stage(
            agent_profile, curriculum_record, config, run_dir, skip_judge, verbose,
            progress_tracker=progress_tracker,
        )
        step += 1

    # ── Behavioral Testing ──
    if include_behavioral:
        try:
            _track("behavioral", 0)
            if verbose:
                print(f"Step {step}/{total_steps}: Running adversarial scenarios against agent...")

            def behavioral_progress_cb(s_id, index, total):
                if verbose:
                    print_progress(s_id, index, total)
                _track("behavioral", index, total)

            transcript = await run_scenarios(
                agent_profile=agent_profile,
                scenario_dir=scenario_dir,
                config=config,
                progress_callback=behavioral_progress_cb,
            )
            transcript_path = save_transcript(transcript, run_dir)

            if transcript.get("aborted"):
                print(f"\n  ⚠ Run aborted: Too many connection failures")

            if verbose:
                completed = transcript["scenarios_completed"]
                applicable = transcript.get("scenarios_applicable", transcript["scenarios_total"])
                na = transcript.get("scenarios_not_applicable", 0)
                print(f"  ✓ {completed}/{applicable} applicable scenarios completed ({na} skipped by IAP)")
                print()

            # ── Tool Boundary Scenarios (MCP servers) ──
            iap = agent_profile.get("iap", {})
            if iap.get("interface_type") == "mcp_server":
                tb_scenarios = load_tool_boundary_scenarios(scenario_dir)
                if tb_scenarios:
                    if verbose:
                        print(f"  Running tool boundary scenarios against MCP server...")
                    from runner.mcp_adapter import MCPAdapter
                    mcp_adapter = MCPAdapter(config)
                    tb_runner = ToolBoundaryRunner(mcp_adapter, agent_profile, tb_scenarios, config)
                    tb_result = await tb_runner.run_all(
                        progress_callback=print_progress if verbose else None,
                    )
                    tb_entries = tb_result.get("transcript", [])
                    transcript["transcript"].extend(tb_entries)
                    tb_completed = sum(1 for t in tb_entries if t["status"] == "completed")
                    tb_total = len(tb_entries)
                    transcript["scenarios_total"] += tb_total
                    transcript["scenarios_applicable"] = transcript.get("scenarios_applicable", 0) + tb_total
                    transcript["scenarios_completed"] += tb_completed
                    if verbose:
                        print(f"  ✓ {tb_completed}/{tb_total} tool boundary scenarios completed")
                        print()
        except Exception as e:
            import traceback
            print(f"  [ERROR] Behavioral testing failed: {e}")
            traceback.print_exc()
            if transcript is None:
                transcript = {
                    "scenarios_total": 0,
                    "scenarios_completed": 0,
                    "scenarios_applicable": 0,
                    "scenarios_not_applicable": 0,
                    "transcript": [],
                    "aborted": False,
                    "behavioral_error": str(e),
                }
                save_transcript(transcript, run_dir)

        step += 1

    # ── Evaluation ──
    if verbose:
        print(f"Step {step}/{total_steps}: Evaluating responses and computing scores...")

    if include_behavioral and transcript and transcript.get("transcript"):
        try:
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
        except Exception as e:
            import traceback
            print(f"  [ERROR] Behavioral evaluation failed: {e}")
            traceback.print_exc()
            evaluation_results = {"evaluations": [], "run_id": "unknown",
                                  "agent_name": agent_name, "evaluation_error": str(e)}

    step += 1

    # ── Scoring ──
    _track("scoring")
    # Use composite scoring for v0.5 paths, legacy scoring for adversarial-only
    try:
        if assessment_path == "adversarial_only" and evaluation_results:
            # Legacy v0.4 scoring for backward compatibility
            certification_score = compute_certification_score(evaluation_results, config)
            # Add v0.5 fields for report compatibility
            certification_score["composite_score"] = certification_score.get("overall_score", 0)
            certification_score["exam_score"] = None
            certification_score["behavioral_score"] = certification_score.get("overall_score", 0)
            certification_score["boundary_score"] = None
            certification_score["diagnostic"] = None
            certification_score["certification_level"] = {
                "level": 0, "name": "Behavioral Only",
                "description": "Adversarial testing without knowledge assessment"
            }
        else:
            # v0.5 composite scoring
            from scoring.composite_scorer import compute_composite_score, save_composite_score

            if evaluation_results is None:
                evaluation_results = {"evaluations": [], "run_id": "unknown", "agent_name": agent_name}

            certification_score = compute_composite_score(
                exam_results=exam_results or {},
                evaluation_results=evaluation_results,
                agent_profile=agent_profile,
                config=config,
            )
            # Also keep legacy fields for compatibility
            certification_score["overall_score"] = certification_score.get("composite_score", 0)
            certification_score["run_id"] = evaluation_results.get("run_id", "unknown")
            certification_score["agent_name"] = agent_name
    except Exception as e:
        import traceback
        print(f"  [ERROR] Scoring failed: {e}")
        traceback.print_exc()
        certification_score = {
            "composite_score": 0, "overall_score": 0, "decision": "ERROR",
            "exam_score": exam_results.get("overall_score_pct", 0) if exam_results else None,
            "behavioral_score": None, "boundary_score": None,
            "diagnostic": None, "scoring_error": str(e),
            "certification_level": {"level": 0, "name": "Scoring Error",
                                    "description": f"Scoring failed: {e}"},
            "run_id": "unknown", "agent_name": agent_name,
        }

    score_path = save_certification_score(certification_score, run_dir)

    if verbose:
        composite = certification_score.get("composite_score", certification_score.get("overall_score", 0))
        decision = certification_score.get("decision", "UNKNOWN")
        level = certification_score.get("certification_level", {})
        icon = "✓" if decision == "PASS" else ("⚠" if decision == "CONDITIONAL" else "✗")
        print(f"  {icon} Composite Score: {composite}/100 — {decision}")
        if level:
            print(f"  Level {level.get('level', 0)}: {level.get('name', 'N/A')}")

        if exam_results:
            print(f"    Knowledge Exam: {exam_results.get('overall_score_pct', 0)}%")
        behavioral = certification_score.get("behavioral_score")
        if behavioral is not None:
            print(f"    Behavioral: {behavioral}/100")
        boundary = certification_score.get("boundary_score")
        if boundary is not None:
            print(f"    Tool Boundary: {boundary}%")

        diagnostic = certification_score.get("diagnostic")
        if diagnostic and diagnostic.get("label"):
            print(f"    Diagnostic: {diagnostic['label']}")
        print()

    # ── Report ──
    if verbose:
        print(f"Step {step}/{total_steps}: Generating certification report...")

    # Build report context
    report_md = generate_report(
        agent_profile=agent_profile,
        certification_score=certification_score,
        evaluation_results=evaluation_results or {"evaluations": []},
        transcript=transcript or {"scenarios_total": 0, "scenarios_completed": 0, "scenarios_applicable": 0},
        config=config,
        curriculum_record=curriculum_record,
        exam_results=exam_results,
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
        print(f"    • Agent Profile:       {profile_path}")
        if curriculum_record:
            print(f"    • Curriculum Record:   {run_dir}/curriculum_record.json")
        if exam_results:
            print(f"    • Exam Results:        {run_dir}/exam_results.json")
        if transcript:
            print(f"    • Test Transcript:     {run_dir}/test_transcript.json")
        if evaluation_results and include_behavioral:
            print(f"    • Evaluation Results:  {run_dir}/evaluation_results.json")
        print(f"    • Certification Score: {score_path}")
        print(f"    • Report:              {report_path}")
        print()

    return {
        "run_dir": run_dir,
        "agent_profile": agent_profile,
        "curriculum_record": curriculum_record,
        "exam_results": exam_results,
        "transcript": transcript,
        "evaluation_results": evaluation_results,
        "certification_score": certification_score,
        "report_path": report_path,
    }


def main():
    parser = argparse.ArgumentParser(
        description="TAISE-Agent v0.5 Certification Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Assessment Paths:
  full_certification  Curriculum + Exam + Adversarial Testing (recommended)
  education_exam      Curriculum + Exam only (Level 1 eligible)
  adversarial_only    Adversarial testing only (v0.4 compatible)

Examples:
  python run_certification.py --agent agents/my_agent.json
  python run_certification.py --agent agents/my_agent.json --path full_certification
  python run_certification.py --agent agents/my_agent.json --path education_exam
  python run_certification.py --agent agents/my_agent.json --path adversarial_only --skip-judge
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
        "--path", default="full_certification",
        choices=["full_certification", "education_exam", "adversarial_only"],
        help="Assessment path (default: full_certification)",
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
        assessment_path=args.path,
    ))

    # Exit with appropriate code
    decision = result["certification_score"]["decision"]
    sys.exit(0 if decision == "PASS" else 1)


if __name__ == "__main__":
    main()
