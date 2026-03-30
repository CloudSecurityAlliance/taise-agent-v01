#!/usr/bin/env python3
"""One-time migration: seed data/ directory from existing exam questions and scenarios.

Idempotent — skips items that already exist.

Usage:
    python scripts/migrate_data.py
"""

import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = Path(os.environ.get("TAISE_DATA_DIR", str(PROJECT_ROOT / "data")))


def migrate_exam():
    """Consolidate exam/questions/*.yaml into data/exams/taise-agent-safety/questions.yaml."""
    exam_id = "taise-agent-safety"
    target_dir = DATA_DIR / "exams" / exam_id
    target_file = target_dir / "questions.yaml"

    if target_file.exists():
        print(f"  [SKIP] {target_file} already exists")
        return

    questions_dir = PROJECT_ROOT / "exam" / "questions"
    if not questions_dir.exists():
        print(f"  [WARN] {questions_dir} not found — skipping exam migration")
        return

    # Load all individual question files
    questions = []
    category_counter = Counter()
    for yaml_file in sorted(questions_dir.glob("*.yaml")):
        with open(yaml_file) as f:
            q = yaml.safe_load(f)
            if q and isinstance(q, dict) and "question_id" in q:
                questions.append(q)
                category_counter[q.get("category", "unknown")] += 1

    if not questions:
        print("  [WARN] No questions found — skipping")
        return

    # Build category list with weights
    num_cats = len(category_counter)
    base_weight = 100 // num_cats if num_cats else 100
    remainder = 100 - (base_weight * num_cats)
    categories = []
    for i, (cat_id, count) in enumerate(sorted(category_counter.items())):
        w = base_weight + (1 if i < remainder else 0)
        categories.append({
            "id": cat_id,
            "name": cat_id.replace("_", " ").title(),
            "weight": w,
            "question_count": count,
        })

    consolidated = {
        "exam_metadata": {
            "exam_id": exam_id,
            "exam_name": "TAISE-Agent Safety Exam",
            "version": "0.5",
            "description": "Core TAISE-Agent behavioral safety knowledge exam covering authority boundaries, data protection, prompt injection, truthfulness, escalation, and tool safety.",
            "author": "Cloud Security Alliance",
            "created_date": "2026-03-14",
            "categories": categories,
        },
        "questions": questions,
    }

    target_dir.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w") as f:
        yaml.dump(consolidated, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"  [OK] Migrated {len(questions)} questions -> {target_file}")

    # Create/update exam registry
    registry_path = DATA_DIR / "exams" / "registry.yaml"
    existing = []
    if registry_path.exists():
        with open(registry_path) as f:
            existing = (yaml.safe_load(f) or {}).get("exams", [])

    if any(e["exam_id"] == exam_id for e in existing):
        print(f"  [SKIP] Exam '{exam_id}' already in registry")
    else:
        existing.append({
            "exam_id": exam_id,
            "exam_name": "TAISE-Agent Safety Exam",
            "description": "Core TAISE-Agent behavioral safety knowledge exam",
            "question_file": f"{exam_id}/questions.yaml",
            "question_count": len(questions),
            "categories": num_cats,
            "uploaded_at": "2026-03-14T00:00:00Z",
            "visible": True,
            "is_default": True,
        })
        with open(registry_path, "w") as f:
            yaml.dump({"exams": existing}, f, default_flow_style=False, sort_keys=False)
        print(f"  [OK] Added '{exam_id}' to exam registry")


def migrate_suite():
    """Copy scenarios/ tree into data/suites/taise-v05-core/."""
    suite_id = "taise-v05-core"
    target_dir = DATA_DIR / "suites" / suite_id
    source_dir = PROJECT_ROOT / "scenarios"

    if target_dir.exists() and any(target_dir.rglob("*.yaml")):
        print(f"  [SKIP] {target_dir} already populated")
    elif not source_dir.exists():
        print(f"  [WARN] {source_dir} not found — skipping suite migration")
        return
    else:
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        scenario_count = len(list(target_dir.rglob("*.yaml")))
        categories = set()
        for d in target_dir.iterdir():
            if d.is_dir():
                categories.add(d.name)
        print(f"  [OK] Copied {scenario_count} scenarios ({len(categories)} categories) -> {target_dir}")

    # Count scenarios for registry
    scenario_count = len(list(target_dir.rglob("*.yaml")))
    categories = set()
    for d in target_dir.iterdir():
        if d.is_dir():
            categories.add(d.name)

    # Create/update suite registry
    registry_path = DATA_DIR / "suites" / "registry.yaml"
    existing = []
    if registry_path.exists():
        with open(registry_path) as f:
            existing = (yaml.safe_load(f) or {}).get("suites", [])

    if any(s["suite_id"] == suite_id for s in existing):
        print(f"  [SKIP] Suite '{suite_id}' already in registry")
    else:
        existing.append({
            "suite_id": suite_id,
            "suite_name": f"TAISE v0.5 Core ({scenario_count} scenarios)",
            "description": "Standard TAISE-Agent adversarial test suite",
            "scenario_dir": suite_id,
            "scenario_count": scenario_count,
            "categories": len(categories),
            "uploaded_at": "2026-03-14T00:00:00Z",
            "is_active": True,
        })
        with open(registry_path, "w") as f:
            yaml.dump({"suites": existing}, f, default_flow_style=False, sort_keys=False)
        print(f"  [OK] Added '{suite_id}' to suite registry")


def main():
    print(f"TAISE-Agent Data Migration")
    print(f"  Data directory: {DATA_DIR}")
    print()
    print("Migrating exam questions...")
    migrate_exam()
    print()
    print("Migrating adversarial scenarios...")
    migrate_suite()
    print()
    print("Migration complete.")


if __name__ == "__main__":
    main()
