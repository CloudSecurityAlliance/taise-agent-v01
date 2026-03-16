#!/usr/bin/env bash
# ------------------------------------------------------------------
# TAISE-Agent v0.2 — Digital Ocean Droplet Setup
# Checks and installs all system + Python dependencies.
# Tested on Ubuntu 22.04 / 24.04 LTS.
#
# Usage:  chmod +x setup_droplet.sh && sudo ./setup_droplet.sh
# ------------------------------------------------------------------

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC}  $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC}  $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC}  $1"; }

REQUIRED_PYTHON="3.11"
PROJECT_DIR="/root/taise-agent-v01"
VENV_DIR="${PROJECT_DIR}/venv"

echo "============================================"
echo "  TAISE-Agent v0.2 — Droplet Setup"
echo "============================================"
echo ""

# ------------------------------------------------------------------
# 1. System packages
# ------------------------------------------------------------------
echo "--- System packages ---"

apt-get update -qq

SYSTEM_PKGS=(
    python3              # Runtime
    python3-venv         # venv support (not always bundled)
    python3-pip          # pip bootstrap
    python3-dev          # C extension headers (httpx, pydantic, etc.)
    build-essential      # gcc / make for compiled wheels
    libffi-dev           # cffi (cryptography, pyjwt)
    libssl-dev           # OpenSSL headers (httpx, anyio TLS)
    git                  # Version control
    curl                 # Health checks, API testing
    jq                   # JSON processing in shell scripts
)

for pkg in "${SYSTEM_PKGS[@]}"; do
    if dpkg -s "$pkg" &>/dev/null; then
        ok "$pkg"
    else
        warn "$pkg missing — installing..."
        apt-get install -y -qq "$pkg"
        ok "$pkg installed"
    fi
done

echo ""

# ------------------------------------------------------------------
# 2. Python version check
# ------------------------------------------------------------------
echo "--- Python version ---"

PYTHON_BIN=$(command -v python3 || true)
if [[ -z "$PYTHON_BIN" ]]; then
    fail "python3 not found"
    exit 1
fi

PY_VERSION=$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PYTHON_BIN -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON_BIN -c 'import sys; print(sys.version_info.minor)')

if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
    ok "Python $PY_VERSION (>= $REQUIRED_PYTHON)"
else
    fail "Python $PY_VERSION found — need >= $REQUIRED_PYTHON"
    warn "Install with: apt install python3.12 python3.12-venv python3.12-dev"
    exit 1
fi

echo ""

# ------------------------------------------------------------------
# 3. Project directory
# ------------------------------------------------------------------
echo "--- Project directory ---"

if [[ -d "$PROJECT_DIR" ]]; then
    ok "$PROJECT_DIR exists"
else
    warn "Creating $PROJECT_DIR"
    mkdir -p "$PROJECT_DIR"
    ok "$PROJECT_DIR created"
fi

echo ""

# ------------------------------------------------------------------
# 4. Virtual environment
# ------------------------------------------------------------------
echo "--- Virtual environment ---"

if [[ -f "${VENV_DIR}/bin/activate" ]]; then
    ok "venv at $VENV_DIR"
else
    warn "Creating venv..."
    $PYTHON_BIN -m venv "$VENV_DIR"
    ok "venv created"
fi

# Activate
source "${VENV_DIR}/bin/activate"

# Upgrade pip
pip install --upgrade pip setuptools wheel -q
ok "pip $(pip --version | awk '{print $2}')"

echo ""

# ------------------------------------------------------------------
# 5. Python dependencies
# ------------------------------------------------------------------
echo "--- Python packages ---"

# Core requirements from requirements.txt
PIP_PACKAGES=(
    "fastapi>=0.109.0"
    "uvicorn>=0.27.0"
    "httpx>=0.26.0"
    "pyyaml>=6.0.1"
    "jinja2>=3.1.3"
    "anthropic>=0.45.0"
    "openai>=1.12.0"
    "python-dotenv>=1.0.0"
    "pydantic>=2.6.0"
    "python-telegram-bot>=21.0"
)

# MCP SDK (not in requirements.txt but imported by mcp_adapter.py)
PIP_PACKAGES+=("mcp[cli]>=1.26.0")

# Telethon (used by telegram_adapter.py for MTProto)
PIP_PACKAGES+=("telethon>=1.0")

pip install -q "${PIP_PACKAGES[@]}"
ok "All Python packages installed"

# Verify critical imports
echo ""
echo "--- Import verification ---"

IMPORTS=(
    "fastapi:fastapi"
    "uvicorn:uvicorn"
    "httpx:httpx"
    "yaml:pyyaml"
    "jinja2:jinja2"
    "anthropic:anthropic"
    "openai:openai"
    "dotenv:python-dotenv"
    "pydantic:pydantic"
    "mcp:mcp SDK"
    "mcp.client.streamable_http:mcp streamable_http"
)

for entry in "${IMPORTS[@]}"; do
    mod="${entry%%:*}"
    label="${entry##*:}"
    if "${VENV_DIR}/bin/python" -c "import ${mod}" 2>/dev/null; then
        ok "$label"
    else
        fail "$label — 'import ${mod}' failed"
    fi
done

echo ""

# ------------------------------------------------------------------
# 6. Environment variables check
# ------------------------------------------------------------------
echo "--- Environment variables ---"

ENV_FILE="${PROJECT_DIR}/.env"

REQUIRED_VARS=(
    "ANTHROPIC_API_KEY:Required — Claude judge for evaluation"
)

OPTIONAL_VARS=(
    "OPENAI_API_KEY:Optional — if using OpenAI as judge"
    "TELEGRAM_API_ID:Optional — Telegram adapter"
    "TELEGRAM_API_HASH:Optional — Telegram adapter"
    "TELEGRAM_BOT_TOKEN:Optional — Telegram adapter"
    "OPENCLAW_HOOK_TOKEN:Optional — OpenClaw webhook auth"
)

for entry in "${REQUIRED_VARS[@]}"; do
    var="${entry%%:*}"
    desc="${entry##*:}"
    if [[ -n "${!var:-}" ]]; then
        ok "$var is set"
    elif [[ -f "$ENV_FILE" ]] && grep -q "^${var}=" "$ENV_FILE" 2>/dev/null; then
        ok "$var in .env file"
    else
        warn "$var not set — $desc"
    fi
done

for entry in "${OPTIONAL_VARS[@]}"; do
    var="${entry%%:*}"
    desc="${entry##*:}"
    if [[ -n "${!var:-}" ]] || { [[ -f "$ENV_FILE" ]] && grep -q "^${var}=" "$ENV_FILE" 2>/dev/null; }; then
        ok "$var"
    else
        echo -e "  ${YELLOW}[-]${NC}  $var — $desc"
    fi
done

echo ""

# ------------------------------------------------------------------
# 7. Firewall / port check
# ------------------------------------------------------------------
echo "--- Network ---"

# FastAPI submission API default port
API_PORT=8000

if command -v ufw &>/dev/null; then
    if ufw status | grep -q "${API_PORT}"; then
        ok "Port $API_PORT allowed in ufw"
    else
        warn "Port $API_PORT not in ufw — run: ufw allow $API_PORT/tcp"
    fi
else
    ok "ufw not active (check cloud firewall rules for port $API_PORT)"
fi

echo ""

# ------------------------------------------------------------------
# 8. Create .env template if missing
# ------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "--- Creating .env template ---"
    cat > "$ENV_FILE" << 'ENVEOF'
# TAISE-Agent v0.2 Environment Variables
# Fill in values and restart the service.

# Required — Claude judge
ANTHROPIC_API_KEY=

# Optional — OpenAI as alternative judge
# OPENAI_API_KEY=

# Optional — Telegram adapter
# TELEGRAM_API_ID=
# TELEGRAM_API_HASH=
# TELEGRAM_BOT_TOKEN=

# Optional — OpenClaw webhook
# OPENCLAW_HOOK_TOKEN=
ENVEOF
    chmod 600 "$ENV_FILE"
    ok "Created $ENV_FILE (mode 600) — fill in your keys"
    echo ""
fi

# ------------------------------------------------------------------
# 9. Summary
# ------------------------------------------------------------------
echo "============================================"
echo "  Setup complete"
echo "============================================"
echo ""
echo "  Project dir : $PROJECT_DIR"
echo "  Python      : $($PYTHON_BIN --version)"
echo "  Venv        : $VENV_DIR"
echo "  Env file    : $ENV_FILE"
echo ""
echo "  Next steps:"
echo "    1. Edit $ENV_FILE with your API keys"
echo "    2. scp your project files to $PROJECT_DIR/"
echo "    3. Activate:  source $VENV_DIR/bin/activate"
echo "    4. Run:       python run_certification.py --help"
echo "    5. Or API:    uvicorn pod_integration.submission_api:app --host 0.0.0.0 --port 8000"
echo ""
