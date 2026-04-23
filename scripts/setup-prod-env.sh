#!/usr/bin/env bash
# DEPRECATED: This script is replaced by AWS SSM + scripts/fetch-prod-secrets.sh
# See docs/workflow.md for the current prod secrets workflow.
# See docs/runbooks/rotate-db-password.md for rotation instructions.
set -euo pipefail
echo ""
echo "[DEPRECATED] setup-prod-env.sh is deprecated."
echo ""
echo "Prod secrets are now managed via AWS SSM Parameter Store."
echo "  - Fetch secrets: scripts/fetch-prod-secrets.sh"
echo "  - Full deploy:   scripts/deploy-prod.sh"
echo "  - Rotation:      docs/runbooks/rotate-db-password.md"
echo ""
exit 0
