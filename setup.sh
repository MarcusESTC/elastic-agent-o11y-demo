#!/usr/bin/env bash
# setup.sh — One-shot data load for the Elastic AI Agent Observability demo
#
# Usage:
#   export ES_ENDPOINT="https://<project>.es.<region>.elastic.cloud"
#   export ES_API_KEY="<base64 key>"
#   export KIBANA_ENDPOINT="https://<project>.kb.<region>.elastic.cloud"
#   bash setup.sh
#
# Or source your env file first:
#   source .env && bash setup.sh

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗"
echo -e "║     Elastic AI Agent Observability — Setup                   ║"
echo -e "╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Validate env vars ─────────────────────────────────────────────────────────
if [[ -z "${ES_ENDPOINT:-}" || -z "${ES_API_KEY:-}" ]]; then
  echo -e "${RED}ERROR: ES_ENDPOINT and ES_API_KEY must be set.${NC}"
  echo "  export ES_ENDPOINT=\"https://<project>.es.<region>.elastic.cloud\""
  echo "  export ES_API_KEY=\"<base64 key>\""
  exit 1
fi

if [[ -z "${KIBANA_ENDPOINT:-}" ]]; then
  # Auto-derive from ES endpoint
  export KIBANA_ENDPOINT="${ES_ENDPOINT/.es./.kb.}"
  echo -e "${YELLOW}KIBANA_ENDPOINT not set — derived: ${KIBANA_ENDPOINT}${NC}"
fi

echo -e "${GREEN}✓${NC} ES_ENDPOINT:      ${ES_ENDPOINT}"
echo -e "${GREEN}✓${NC} KIBANA_ENDPOINT:  ${KIBANA_ENDPOINT}"
echo ""

# ── Step 1: Generate APM trace data ──────────────────────────────────────────
echo -e "${BLUE}[1/4] Generating APM traces for 4 AI agent services...${NC}"
python3 data/generate_ai_agents.py
echo ""

# ── Step 2: Fill Errors + Logs tabs ──────────────────────────────────────────
echo -e "${BLUE}[2/4] Populating APM Errors and Logs tabs...${NC}"
python3 data/fill_apm_tabs.py
echo ""

# ── Step 3: Fill Dependencies, Infrastructure, Alerts, fix Latency ───────────
echo -e "${BLUE}[3/4] Populating Dependencies, Infrastructure, Alerts...${NC}"
python3 data/fill_apm_complete.py
echo ""

# ── Step 4: Import Kibana LLM dashboard ──────────────────────────────────────
echo -e "${BLUE}[4/4] Importing LLM Observability dashboard to Kibana...${NC}"
python3 data/build_llm_dashboard.py
echo ""

echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗"
echo -e "║  ✅  Setup complete!                                         ║"
echo -e "╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Next steps:"
echo "  1. Open Kibana APM:        ${KIBANA_ENDPOINT}/app/apm"
echo "  2. Open LLM dashboard:     ${KIBANA_ENDPOINT}/app/dashboards"
echo "  3. Open slide deck:        open slides/ai_obs_deck.html"
echo "  4. Read the demo script:   docs/demo_script.md"
echo ""
echo "To refresh data (run every 2 days):"
echo "  python3 data/generate_ai_agents.py && python3 data/fill_apm_complete.py"
