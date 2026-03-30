"""TAISE-Agent v0.5 — Admin API

Provides admin endpoints for managing exams and adversarial test suites,
plus public endpoints for listing visible exams.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from pod_integration.registry import (
    extract_suite_zip,
    get_active_suite,
    get_data_dir,
    get_default_exam,
    get_exam_by_id,
    get_exam_dir,
    get_exam_questions_path,
    get_suite_by_id,
    get_suite_dir,
    get_visible_exams,
    load_exam_registry,
    load_suite_registry,
    save_exam_registry,
    save_suite_registry,
    slugify,
    validate_exam_file,
    validate_suite_zip,
)


# ── Auth Dependency ──

def require_admin(x_admin_secret: str = Header(...)):
    """Verify the admin secret header."""
    expected = os.environ.get("TAISE_ADMIN_SECRET")
    if not expected:
        raise HTTPException(503, detail="Admin not configured. Set TAISE_ADMIN_SECRET environment variable.")
    if x_admin_secret != expected:
        raise HTTPException(401, detail="Invalid admin secret")


admin_router = APIRouter(tags=["admin"])


# ── Public Endpoints ──

@admin_router.get("/agent-cert/exams")
async def list_visible_exams():
    """Return visible exams for the main page dropdown."""
    exams = get_visible_exams()
    return {
        "exams": [
            {
                "exam_id": e["exam_id"],
                "exam_name": e.get("exam_name", e["exam_id"]),
                "description": e.get("description", ""),
                "question_count": e.get("question_count", 0),
                "categories": e.get("categories", 0),
                "is_default": e.get("is_default", False),
            }
            for e in exams
        ]
    }


@admin_router.get("/agent-cert/active-suite")
async def get_active_suite_info():
    """Return info about the currently active adversarial test suite."""
    suite = get_active_suite()
    if not suite:
        return {"suite": None}
    return {
        "suite": {
            "suite_id": suite["suite_id"],
            "suite_name": suite.get("suite_name", suite["suite_id"]),
            "description": suite.get("description", ""),
            "scenario_count": suite.get("scenario_count", 0),
            "categories": suite.get("categories", 0),
        }
    }


# ── Admin: Login ──

@admin_router.post("/admin/login")
async def admin_login(x_admin_secret: str = Header(...)):
    """Validate admin secret. Returns 200 on success, 401 on failure."""
    expected = os.environ.get("TAISE_ADMIN_SECRET")
    if not expected:
        raise HTTPException(503, detail="Admin not configured. Set TAISE_ADMIN_SECRET environment variable.")
    if x_admin_secret != expected:
        raise HTTPException(401, detail="Invalid admin secret")
    return {"ok": True}


# ── Admin: Exams ──

@admin_router.get("/admin/exams", dependencies=[Depends(require_admin)])
async def admin_list_exams():
    """Return all exams (including non-visible) with full metadata."""
    exams = load_exam_registry()
    return {"exams": exams}


@admin_router.post("/admin/exams", dependencies=[Depends(require_admin)])
async def admin_upload_exam(
    exam_name: str = Form(...),
    description: str = Form(""),
    visible: bool = Form(True),
    is_default: bool = Form(False),
    question_file: UploadFile = File(...),
):
    """Upload a new exam question bank."""
    # Validate file extension
    filename = question_file.filename or ""
    if not filename.lower().endswith((".yaml", ".yml", ".json")):
        raise HTTPException(400, detail={"errors": ["File must be .yaml, .yml, or .json"]})

    # Read and size-check
    content_bytes = await question_file.read()
    if len(content_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, detail={"errors": ["File exceeds 10MB limit"]})

    # Parse
    try:
        if filename.lower().endswith(".json"):
            parsed = json.loads(content_bytes)
        else:
            parsed = yaml.safe_load(content_bytes)
    except Exception as e:
        raise HTTPException(400, detail={"errors": [f"Failed to parse file: {e}"]})

    if not isinstance(parsed, dict):
        raise HTTPException(400, detail={"errors": ["File must contain a YAML/JSON mapping"]})

    # Validate format
    errors = validate_exam_file(parsed)
    if errors:
        raise HTTPException(400, detail={"errors": errors})

    # Derive exam_id
    metadata = parsed.get("exam_metadata", {})
    exam_id = metadata.get("exam_id") or slugify(exam_name)

    # Check for duplicate
    if get_exam_by_id(exam_id):
        raise HTTPException(409, detail={"errors": [f"Exam '{exam_id}' already exists"]})

    # Save file
    exam_dir = get_exam_dir(exam_id)
    exam_dir.mkdir(parents=True, exist_ok=True)
    questions_path = exam_dir / "questions.yaml"
    with open(questions_path, "w") as f:
        yaml.dump(parsed, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Count questions and categories
    questions = parsed.get("questions", [])
    cats = set(q.get("category", "") for q in questions)

    # Update registry
    exams = load_exam_registry()

    if is_default:
        for e in exams:
            e["is_default"] = False

    exams.append({
        "exam_id": exam_id,
        "exam_name": exam_name,
        "description": description,
        "question_file": f"{exam_id}/questions.yaml",
        "question_count": len(questions),
        "categories": len(cats),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "visible": visible,
        "is_default": is_default,
    })
    save_exam_registry(exams)

    return {"ok": True, "exam_id": exam_id, "question_count": len(questions), "categories": len(cats)}


@admin_router.put("/admin/exams/{exam_id}", dependencies=[Depends(require_admin)])
async def admin_update_exam(
    exam_id: str,
    exam_name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    visible: Optional[bool] = Form(None),
    is_default: Optional[bool] = Form(None),
    question_file: Optional[UploadFile] = File(None),
):
    """Update exam metadata or replace question file."""
    exams = load_exam_registry()
    entry = None
    for e in exams:
        if e["exam_id"] == exam_id:
            entry = e
            break
    if not entry:
        raise HTTPException(404, detail=f"Exam '{exam_id}' not found")

    # Update metadata fields if provided
    if exam_name is not None:
        entry["exam_name"] = exam_name
    if description is not None:
        entry["description"] = description

    if visible is not None:
        # Guard: don't hide if it's the last visible exam
        if not visible:
            visible_count = sum(1 for e in exams if e.get("visible", True) and e["exam_id"] != exam_id)
            if visible_count < 1:
                raise HTTPException(400, detail="Cannot hide the last visible exam")
        entry["visible"] = visible

    if is_default is not None and is_default:
        for e in exams:
            e["is_default"] = False
        entry["is_default"] = True

    # Replace question file if provided
    if question_file:
        filename = question_file.filename or ""
        if not filename.lower().endswith((".yaml", ".yml", ".json")):
            raise HTTPException(400, detail={"errors": ["File must be .yaml, .yml, or .json"]})

        content_bytes = await question_file.read()
        if len(content_bytes) > 10 * 1024 * 1024:
            raise HTTPException(400, detail={"errors": ["File exceeds 10MB limit"]})

        try:
            if filename.lower().endswith(".json"):
                parsed = json.loads(content_bytes)
            else:
                parsed = yaml.safe_load(content_bytes)
        except Exception as e:
            raise HTTPException(400, detail={"errors": [f"Failed to parse file: {e}"]})

        errors = validate_exam_file(parsed)
        if errors:
            raise HTTPException(400, detail={"errors": errors})

        questions_path = get_exam_questions_path(exam_id)
        questions_path.parent.mkdir(parents=True, exist_ok=True)
        with open(questions_path, "w") as f:
            yaml.dump(parsed, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        questions = parsed.get("questions", [])
        cats = set(q.get("category", "") for q in questions)
        entry["question_count"] = len(questions)
        entry["categories"] = len(cats)

    save_exam_registry(exams)
    return {"ok": True, "exam": entry}


@admin_router.delete("/admin/exams/{exam_id}", dependencies=[Depends(require_admin)])
async def admin_delete_exam(exam_id: str):
    """Delete an exam."""
    exams = load_exam_registry()
    entry = None
    for e in exams:
        if e["exam_id"] == exam_id:
            entry = e
            break
    if not entry:
        raise HTTPException(404, detail=f"Exam '{exam_id}' not found")

    if entry.get("is_default"):
        raise HTTPException(400, detail="Cannot delete the default exam. Set another exam as default first.")

    visible_count = sum(1 for e in exams if e.get("visible", True))
    if visible_count <= 1 and entry.get("visible", True):
        raise HTTPException(400, detail="Cannot delete the last visible exam")

    # Remove from registry
    exams = [e for e in exams if e["exam_id"] != exam_id]
    save_exam_registry(exams)

    # Remove files
    exam_dir = get_exam_dir(exam_id)
    if exam_dir.exists():
        shutil.rmtree(exam_dir)

    return {"ok": True, "deleted": exam_id}


# ── Admin: Suites ──

@admin_router.get("/admin/suites", dependencies=[Depends(require_admin)])
async def admin_list_suites():
    """Return all adversarial test suites."""
    suites = load_suite_registry()
    return {"suites": suites}


@admin_router.post("/admin/suites", dependencies=[Depends(require_admin)])
async def admin_upload_suite(
    suite_name: str = Form(...),
    description: str = Form(""),
    suite_file: UploadFile = File(...),
):
    """Upload a new adversarial test suite ZIP."""
    filename = suite_file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, detail={"errors": ["File must be a .zip archive"]})

    content_bytes = await suite_file.read()
    if len(content_bytes) > 50 * 1024 * 1024:
        raise HTTPException(400, detail={"errors": ["File exceeds 50MB limit"]})

    # Write to temp file for validation
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(content_bytes)
        tmp_path = tmp.name

    try:
        errors, summaries = validate_suite_zip(tmp_path)
        if errors:
            raise HTTPException(400, detail={"errors": errors})

        suite_id = slugify(suite_name)

        # Check for duplicate
        if get_suite_by_id(suite_id):
            raise HTTPException(409, detail={"errors": [f"Suite '{suite_id}' already exists"]})

        # Extract
        suite_dir = get_suite_dir(suite_id)
        scenario_count = extract_suite_zip(tmp_path, suite_dir)

        # Count categories
        categories = set(s["category"] for s in summaries)

        # Update registry
        suites = load_suite_registry()
        suites.append({
            "suite_id": suite_id,
            "suite_name": suite_name,
            "description": description,
            "scenario_dir": suite_id,
            "scenario_count": scenario_count,
            "categories": len(categories),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "is_active": False,
        })
        save_suite_registry(suites)

        return {
            "ok": True,
            "suite_id": suite_id,
            "scenario_count": scenario_count,
            "categories": len(categories),
            "category_breakdown": {cat: sum(1 for s in summaries if s["category"] == cat) for cat in categories},
        }
    finally:
        os.unlink(tmp_path)


@admin_router.put("/admin/suites/{suite_id}/activate", dependencies=[Depends(require_admin)])
async def admin_activate_suite(suite_id: str):
    """Set a suite as the active suite for all certifications."""
    suites = load_suite_registry()
    found = False
    for s in suites:
        if s["suite_id"] == suite_id:
            found = True
            s["is_active"] = True
        else:
            s["is_active"] = False
    if not found:
        raise HTTPException(404, detail=f"Suite '{suite_id}' not found")
    save_suite_registry(suites)
    return {"ok": True, "active_suite": suite_id}


@admin_router.delete("/admin/suites/{suite_id}", dependencies=[Depends(require_admin)])
async def admin_delete_suite(suite_id: str):
    """Delete an inactive suite."""
    suites = load_suite_registry()
    entry = None
    for s in suites:
        if s["suite_id"] == suite_id:
            entry = s
            break
    if not entry:
        raise HTTPException(404, detail=f"Suite '{suite_id}' not found")

    if entry.get("is_active"):
        raise HTTPException(400, detail="Cannot delete the active suite. Activate another suite first.")

    suites = [s for s in suites if s["suite_id"] != suite_id]
    save_suite_registry(suites)

    suite_dir = get_suite_dir(suite_id)
    if suite_dir.exists():
        shutil.rmtree(suite_dir)

    return {"ok": True, "deleted": suite_id}
