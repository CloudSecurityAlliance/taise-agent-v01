# Multi-User Upgrade Plan — TAISE-Agent v0.5

## Current State

The system is single-user by design. There is no authentication, no user identity, no database, and no concurrency protection. All state lives in a global Python dict (`active_runs`) and JSON files on disk. Uvicorn runs as a single process with no worker configuration. Two users submitting within the same second would collide on `run_id` and overwrite each other's results.

---

## Phase 1: Eliminate Collision Risk (Quick Wins)

**Goal:** Make concurrent submissions safe without architectural changes.

### 1.1 — UUID-based run_id
- **File:** `pod_integration/submission_api.py:442`
- **Change:** Replace `run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}"` with `run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"`
- **Also update:** `run_certification.py:56-62` (`create_run_directory`) to use the same scheme
- **Impact:** Eliminates timestamp collision. Zero risk of two runs sharing a directory.

### 1.2 — Concurrency limiter on certification execution
- **File:** `pod_integration/submission_api.py:214-286` (`execute_certification`)
- **Change:** Add an `asyncio.Semaphore(max_concurrent_runs)` (configurable, default 2) that gates entry to `execute_certification`. Additional submissions queue until a slot opens.
- **Also add:** A queue position indicator in `active_runs[run_id]` so the frontend can show "Position 3 in queue"
- **Impact:** Prevents resource exhaustion. CPU/memory-bound LLM judge calls won't compete.

### 1.3 — asyncio.Lock on active_runs mutations
- **File:** `pod_integration/submission_api.py:154` and all 11 write sites (lines 218, 219, 248, 250, 252, 265-276, 282-286, 489-493)
- **Change:** Wrap all `active_runs[run_id]` mutations in `async with runs_lock:` using a module-level `asyncio.Lock()`
- **Impact:** Eliminates dict race conditions for progress tracking.

---

## Phase 2: User Identity & Session Management

**Goal:** Associate runs with users so they only see their own results.

### 2.1 — Lightweight session system
- **Approach:** On first visit, frontend generates a `user_id` (UUID v4) and stores it in `localStorage`. All API requests include it as an `X-User-ID` header.
- **Files to change:**
  - `web/index.html:1402-1423` — Add header to `fetch()` calls
  - `pod_integration/submission_api.py:432` — Read `X-User-ID` from request headers
  - `pod_integration/submission_api.py:489-493` — Store `user_id` in `active_runs[run_id]`
- **Note:** This is identity, not authentication. It prevents accidental cross-user visibility but not malicious access. Full auth is Phase 4.

### 2.2 — Scope all endpoints to user_id
- **Endpoints to update:**
  - `GET /agent-cert/status/{run_id}` (line 520-540) — Return 404 if `user_id` doesn't match
  - `GET /agent-cert/results/{run_id}` (line 559-608) — Same ownership check
  - `GET /agent-cert/report/{run_id}` (line 611-630) — Same ownership check
  - `GET /agent-cert/runs` (line 655-671) — Filter to only runs belonging to requesting user
- **Impact:** Users only see their own runs. Polling someone else's run_id returns 404.

### 2.3 — Persist user_id in all artifacts
- **Files:**
  - `agents/{run_id}_profile.json` (submission_api.py:240-242) — Add `user_id` field
  - `runs/*/agent_profile.json` (run_certification.py:366-368) — Add `user_id` field
  - `runs/*/test_transcript.json` (runner/scenario_runner.py:303-309) — Add `user_id` field
  - `runs/*/certification_score.json` (scoring/scoring_engine.py:294-300) — Add `user_id` field
- **Impact:** Every artifact is traceable to a user. Enables future queries like "show me all runs by user X."

---

## Phase 3: Persistent State & Reliability

**Goal:** Survive server restarts. Stop losing in-flight runs.

### 3.1 — SQLite database for run state
- **New file:** `pod_integration/db.py`
- **Schema:**
  ```sql
  CREATE TABLE runs (
      run_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      agent_name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      phase TEXT,
      exam_progress INTEGER,
      behavioral_progress INTEGER,
      submitted_at TEXT NOT NULL,
      started_at TEXT,
      completed_at TEXT,
      run_dir TEXT,
      error TEXT,
      results_json TEXT  -- JSON blob for certification_score
  );
  CREATE INDEX idx_runs_user_id ON runs(user_id);
  CREATE INDEX idx_runs_status ON runs(status);
  ```
- **Change:** Replace all `active_runs` dict reads/writes with SQLite queries via `aiosqlite`
- **Remove:** The global `active_runs` dict entirely (submission_api.py:154)
- **Impact:** Runs survive restarts. State is queryable. File locking handled by SQLite.

### 3.2 — Run recovery on startup
- **File:** `pod_integration/submission_api.py` (startup event)
- **Change:** On server start, query DB for `status = 'running'`. Mark them as `'interrupted'`. Optionally offer a `/agent-cert/retry/{run_id}` endpoint to re-queue.
- **Impact:** Users aren't left with permanently "running" ghost runs after a crash.

### 3.3 — Run history page
- **File:** `web/index.html`
- **Change:** Add a "My Runs" section that calls `GET /agent-cert/runs` (already filtered by user_id from Phase 2.2). Show a table with run_id, agent_name, status, score, and timestamp. Each row links to full results.
- **Impact:** Users can return to see past certifications without bookmarking run_ids.

---

## Phase 4: Authentication & Security

**Goal:** Prevent unauthorized access and cross-user data leakage.

### 4.1 — API key or OAuth integration
- **Option A (simple):** Issue API keys stored in SQLite. Users enter their key on the frontend. All requests include `Authorization: Bearer <key>`.
- **Option B (production):** OAuth2 via Google/GitHub. Use `authlib` or `python-jose` for JWT validation. Add FastAPI `Depends()` security on all `/agent-cert/` endpoints.
- **Recommendation:** Start with Option A for the droplet deployment. Migrate to Option B if the system moves to production.

### 4.2 — Tighten CORS
- **File:** `pod_integration/submission_api.py:66-72`
- **Change:** Replace `allow_origins=["*"]` with the actual domain(s) serving the frontend.
- **Impact:** Prevents cross-site request forgery from arbitrary origins.

### 4.3 — Rate limiting
- **Change:** Add `slowapi` or a simple in-memory rate limiter. Limit to N submissions per user per hour.
- **Impact:** Prevents abuse (intentional or accidental) from a single user saturating the system.

---

## Phase 5: Scalability & Observability

**Goal:** Support 10+ concurrent users reliably.

### 5.1 — Task queue for certification runs
- **Change:** Replace `BackgroundTasks.add_task()` (submission_api.py:496) with a Celery + Redis task queue.
- **New files:** `pod_integration/tasks.py` (Celery task definitions), `docker-compose.yml` (Redis service)
- **Benefits:** Persistent job queue, configurable concurrency, retry logic, dead-letter handling, visibility into queue depth.
- **Alternative:** If Celery feels heavy, use `arq` (async Redis queue) which integrates naturally with FastAPI.

### 5.2 — Server-Sent Events for progress
- **New endpoint:** `GET /agent-cert/status/{run_id}/stream`
- **Change:** Replace 5-second polling (web/index.html:1502) with an EventSource connection. Server pushes progress updates as they happen.
- **Impact:** Lower latency on progress updates. Reduced server load from polling. Better UX.

### 5.3 — Multi-worker uvicorn
- **File:** `pod_integration/submission_api.py:776-782`
- **Change:** `uvicorn.run(app, host="0.0.0.0", port=8080, workers=4)`
- **Prerequisite:** Phase 3.1 (SQLite) must be complete — workers can't share an in-memory dict.
- **Impact:** HTTP request throughput scales with CPU cores.

### 5.4 — Structured logging
- **Change:** Replace `print()` statements throughout the codebase with `structlog` or `logging` with JSON output. Include `run_id` and `user_id` in every log line.
- **Impact:** Debuggable. Searchable. Ready for log aggregation (Loki, CloudWatch, etc.).

### 5.5 — Nginx reverse proxy
- **New file:** `nginx.conf`
- **Change:** Put nginx in front of uvicorn. Handle TLS termination, static file serving (`web/index.html`), request buffering, and connection limits.
- **Impact:** Production-grade HTTP handling. Enables future horizontal scaling behind a load balancer.

---

## Implementation Priority

| Phase | Effort | Risk Reduction | Recommendation |
|-------|--------|----------------|----------------|
| **Phase 1** | ~2 hours | HIGH | Do immediately — eliminates data loss |
| **Phase 2** | ~3 hours | MEDIUM | Do next — basic user isolation |
| **Phase 3** | ~4 hours | HIGH | Critical for reliability |
| **Phase 4** | ~3 hours | MEDIUM | Required before sharing publicly |
| **Phase 5** | ~6 hours | LOW | Only needed at scale |

**Suggested order:** Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5

Phase 1 alone makes the system safe for a handful of concurrent users on this droplet. Phases 2-3 make it a proper multi-user system. Phases 4-5 are for production readiness.

---

## Files Changed Per Phase (Summary)

| File | Ph.1 | Ph.2 | Ph.3 | Ph.4 | Ph.5 |
|------|------|------|------|------|------|
| `pod_integration/submission_api.py` | X | X | X | X | X |
| `run_certification.py` | X | | | | |
| `web/index.html` | | X | X | | X |
| `runner/scenario_runner.py` | | X | | | |
| `scoring/scoring_engine.py` | | X | | | |
| `pod_integration/db.py` (new) | | | X | | |
| `pod_integration/tasks.py` (new) | | | | | X |
| `config.yaml` | X | | | | |
| `requirements.txt` | | | X | X | X |
| `nginx.conf` (new) | | | | | X |
| `docker-compose.yml` (new) | | | | | X |
