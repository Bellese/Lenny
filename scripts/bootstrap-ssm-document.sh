#!/usr/bin/env bash
set -euo pipefail
REGION=us-east-1
DOC_NAME=leonard-deploy
DOC_FILE="$(dirname "$(realpath "$0")")/../ssm/leonard-deploy-document.json"

# Create or update
if aws ssm describe-document --name "$DOC_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "[+] Updating existing SSM document $DOC_NAME..."
  aws ssm update-document --name "$DOC_NAME" \
    --content "file://$DOC_FILE" \
    --document-version "\$LATEST" \
    --region "$REGION"
  aws ssm update-document-default-version --name "$DOC_NAME" \
    --document-version "\$LATEST" \
    --region "$REGION"
else
  echo "[+] Creating SSM document $DOC_NAME..."
  aws ssm create-document --name "$DOC_NAME" \
    --content "file://$DOC_FILE" \
    --document-type "Command" \
    --document-format "JSON" \
    --region "$REGION"
fi
echo "[+] SSM document $DOC_NAME ready"
