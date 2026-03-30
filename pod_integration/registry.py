"""TAISE-Agent v0.5 — Data Registry

Manages exam and adversarial suite registries stored in data/ directory.
Provides validation for uploaded exam files and scenario suites.
"""

import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml


PROJECT_ROOT = Path(__file__).parent.parent


def get_data_dir() -> Path:
    """Return the root data directory, creating it if needed."""
    data_dir = Path(os.environ.get("TAISE_DATA_DIR", str(PROJECT_ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def slugify(name: str) -> str:
    """Convert a name to a URL-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "unnamed"


# ── Exam Registry ──

def _exam_registry_path() -> Path:
    return get_data_dir() / "exams" / "registry.yaml"


def load_exam_registry() -> list[dict]:
    """Load all exam entries from the registry."""
    path = _exam_registry_path()
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("exams", [])


def save_exam_registry(exams: list[dict]) -> None:
    """Save the exam registry."""
    path = _exam_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"exams": exams}, f, default_flow_style=False, sort_keys=False)


def get_visible_exams() -> list[dict]:
    """Return only exams marked visible."""
    return [e for e in load_exam_registry() if e.get("visible", True)]


def get_default_exam() -> Optional[dict]:
    """Return the default exam entry, or first visible if none marked."""
    exams = load_exam_registry()
    for e in exams:
        if e.get("is_default"):
            return e
    visible = [e for e in exams if e.get("visible", True)]
    return visible[0] if visible else None


def get_exam_by_id(exam_id: str) -> Optional[dict]:
    """Return a specific exam entry by ID."""
    for e in load_exam_registry():
        if e["exam_id"] == exam_id:
            return e
    return None


def get_exam_questions_path(exam_id: str) -> Path:
    """Return the path to a consolidated exam questions file."""
    return get_data_dir() / "exams" / exam_id / "questions.yaml"


def get_exam_dir(exam_id: str) -> Path:
    """Return the directory for an exam."""
    return get_data_dir() / "exams" / exam_id


# ── Suite Registry ──

def _suite_registry_path() -> Path:
    return get_data_dir() / "suites" / "registry.yaml"


def load_suite_registry() -> list[dict]:
    """Load all suite entries from the registry."""
    path = _suite_registry_path()
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("suites", [])


def save_suite_registry(suites: list[dict]) -> None:
    """Save the suite registry."""
    path = _suite_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"suites": suites}, f, default_flow_style=False, sort_keys=False)


def get_active_suite() -> Optional[dict]:
    """Return the active suite entry."""
    for s in load_suite_registry():
        if s.get("is_active"):
            return s
    suites = load_suite_registry()
    return suites[0] if suites else None


def get_suite_by_id(suite_id: str) -> Optional[dict]:
    """Return a specific suite entry by ID."""
    for s in load_suite_registry():
        if s["suite_id"] == suite_id:
            return s
    return None


def get_suite_dir(suite_id: str) -> Path:
    """Return the directory for a suite's scenario files."""
    return get_data_dir() / "suites" / suite_id


# ── Exam File Validation ──

def validate_exam_file(content: dict) -> list[str]:
    """Validate a consolidated exam file. Returns list of error strings (empty = valid)."""
    errors = []

    # 1. exam_metadata section
    metadata = content.get("exam_metadata")
    if not metadata or not isinstance(metadata, dict):
        errors.append("Missing or invalid 'exam_metadata' section")
        return errors  # Can't validate further

    for field in ("exam_id", "exam_name"):
        if not metadata.get(field):
            errors.append(f"exam_metadata missing required field: '{field}'")

    categories_list = metadata.get("categories", [])
    if not categories_list:
        errors.append("exam_metadata missing 'categories' list")

    # Build set of valid category IDs
    valid_categories = set()
    if isinstance(categories_list, list):
        for cat in categories_list:
            if isinstance(cat, dict) and "id" in cat:
                valid_categories.add(cat["id"])
            elif isinstance(cat, dict) and "name" in cat:
                # Allow categories without explicit id — use name-based slug
                valid_categories.add(slugify(cat["name"]))
    elif isinstance(categories_list, dict):
        # Allow dict format: {category_id: name_or_details}
        valid_categories = set(categories_list.keys())

    # Check category weights sum to ~100 if weights are present
    if isinstance(categories_list, list):
        weights = [c.get("weight", 0) for c in categories_list if isinstance(c, dict) and "weight" in c]
        if weights:
            total_weight = sum(weights)
            if abs(total_weight - 100) > 2:
                errors.append(f"Category weights sum to {total_weight}, expected ~100 (tolerance +/-2)")

    # 2. questions array
    questions = content.get("questions")
    if not questions or not isinstance(questions, list):
        errors.append("Missing or empty 'questions' array")
        return errors

    seen_ids = set()
    for i, q in enumerate(questions):
        prefix = f"questions[{i}]"
        if not isinstance(q, dict):
            errors.append(f"{prefix}: not a valid question object")
            continue

        # 3. Required fields
        for field in ("question_id", "category", "question_type", "question_text"):
            if not q.get(field):
                errors.append(f"{prefix}: missing required field '{field}'")

        # 4. Category reference
        cat = q.get("category", "")
        if valid_categories and cat and cat not in valid_categories:
            errors.append(f"{prefix}: category '{cat}' not found in exam_metadata.categories")

        # 5. Multiple choice validation
        q_type = q.get("question_type", "")
        if q_type == "multiple_choice":
            options = q.get("options")
            if not options or not isinstance(options, dict):
                errors.append(f"{prefix}: multiple_choice question missing 'options'")
            elif not all(k in options for k in ("A", "B", "C", "D")):
                errors.append(f"{prefix}: multiple_choice options must include A, B, C, D")
            if not q.get("correct_answer"):
                errors.append(f"{prefix}: multiple_choice question missing 'correct_answer'")

        # 6. Scenario/reasoning questions
        if q_type == "scenario_reasoning" and not q.get("expected_answer_elements"):
            errors.append(f"{prefix}: scenario_reasoning question missing 'expected_answer_elements'")

        # 7. Scoring rubric
        rubric = q.get("scoring_rubric")
        if rubric and isinstance(rubric, dict):
            expected_keys = {0, 1, 2, 3}
            actual_keys = set()
            for k in rubric.keys():
                try:
                    actual_keys.add(int(k))
                except (ValueError, TypeError):
                    pass
            if not expected_keys.issubset(actual_keys):
                missing = expected_keys - actual_keys
                errors.append(f"{prefix}: scoring_rubric missing keys: {missing}")

        # 8. Unique question_id
        qid = q.get("question_id", "")
        if qid:
            if qid in seen_ids:
                errors.append(f"{prefix}: duplicate question_id '{qid}'")
            seen_ids.add(qid)

    return errors


# ── Scenario File Validation ──

def validate_scenario_file(content: dict) -> list[str]:
    """Validate a single scenario YAML file. Returns list of error strings."""
    errors = []
    if not isinstance(content, dict):
        errors.append("Scenario file is not a valid YAML mapping")
        return errors

    for field in ("scenario_id", "category", "name", "severity"):
        if not content.get(field):
            errors.append(f"Missing required field: '{field}'")

    eval_mode = content.get("evaluation_mode", "behavioral")
    if eval_mode == "tool_boundary":
        if not content.get("attack_patterns"):
            errors.append("Tool boundary scenario missing 'attack_patterns'")
    else:
        if not content.get("prompt") and not content.get("turns"):
            errors.append("Behavioral scenario missing 'prompt' (or 'turns' for multi-turn)")

    return errors


def validate_suite_zip(zip_path: str) -> tuple[list[str], list[dict]]:
    """Validate a suite ZIP file.

    Returns (errors, scenario_summaries).
    scenario_summaries is a list of dicts with scenario_id, category, name for valid files.
    """
    errors = []
    summaries = []
    seen_ids = set()

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            yaml_files = [n for n in zf.namelist()
                          if n.endswith((".yaml", ".yml")) and not n.startswith("__MACOSX")]
            if not yaml_files:
                errors.append("ZIP contains no YAML scenario files")
                return errors, summaries

            for name in yaml_files:
                # Security: check for path traversal
                if ".." in name or name.startswith("/"):
                    errors.append(f"Suspicious path in ZIP: '{name}'")
                    continue

                try:
                    content = yaml.safe_load(zf.read(name))
                except Exception as e:
                    errors.append(f"{name}: invalid YAML — {e}")
                    continue

                if not content or not isinstance(content, dict):
                    errors.append(f"{name}: empty or non-mapping YAML")
                    continue

                file_errors = validate_scenario_file(content)
                if file_errors:
                    for err in file_errors:
                        errors.append(f"{name}: {err}")
                    continue

                sid = content["scenario_id"]
                if sid in seen_ids:
                    errors.append(f"{name}: duplicate scenario_id '{sid}'")
                else:
                    seen_ids.add(sid)
                    summaries.append({
                        "scenario_id": sid,
                        "category": content.get("category", "unknown"),
                        "name": content.get("name", ""),
                    })

    except zipfile.BadZipFile:
        errors.append("File is not a valid ZIP archive")

    return errors, summaries


def extract_suite_zip(zip_path: str, target_dir: Path) -> int:
    """Extract a validated suite ZIP into target_dir. Returns scenario count."""
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith((".yaml", ".yml")) and not name.startswith("__MACOSX") and ".." not in name:
                # Preserve subdirectory structure
                target_path = target_dir / name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(zf.read(name))
                count += 1
    return count
