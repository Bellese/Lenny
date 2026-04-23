#!/bin/bash
# One-time setup: create the GitHub Actions OIDC deploy role for Leonard.
#
# Run this from the repo root once before the first GitHub Actions deploy:
#   ./scripts/bootstrap-github-deploy.sh
#
# Prerequisites:
#   - AWS CLI configured with the `leonard` profile (account 439475769170)
#   - Caller must have permissions to manage IAM roles and OIDC providers

set -euo pipefail

AWS_DEFAULT_REGION=us-east-1
export AWS_DEFAULT_REGION

ACCOUNT_ID="439475769170"
REGION="us-east-1"
OIDC_PROVIDER_URL="https://token.actions.githubusercontent.com"
OIDC_THUMBPRINT="6938fd4d98bab03faadb97b34396831e3780aea1"
OIDC_CLIENT_ID="sts.amazonaws.com"
ROLE_NAME="leonard-github-deploy"
POLICY_NAME="leonard-github-deploy-policy"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRUST_POLICY_FILE="$REPO_ROOT/iam/github-deploy-trust-policy.json"
PERMISSION_POLICY_FILE="$REPO_ROOT/iam/leonard-github-deploy-policy.json"

# ---------------------------------------------------------------------------
# Guard: verify we are in the correct AWS account
# ---------------------------------------------------------------------------
echo "Verifying AWS account..."
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
if [ "$CALLER_ACCOUNT" != "$ACCOUNT_ID" ]; then
  echo "ERROR: Expected account $ACCOUNT_ID but got $CALLER_ACCOUNT."
  echo "       Set AWS_PROFILE=leonard (or equivalent) and retry."
  exit 1
fi
echo "  Account OK: $CALLER_ACCOUNT"

# ---------------------------------------------------------------------------
# Step 1: Ensure the GitHub Actions OIDC provider exists
# ---------------------------------------------------------------------------
echo ""
echo "Checking GitHub Actions OIDC provider..."
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" --region "$REGION" >/dev/null 2>&1; then
  echo "  OIDC provider already exists: $OIDC_ARN"
else
  echo "  Creating OIDC provider..."
  aws iam create-open-id-connect-provider \
    --url "$OIDC_PROVIDER_URL" \
    --client-id-list "$OIDC_CLIENT_ID" \
    --thumbprint-list "$OIDC_THUMBPRINT" \
    --region "$REGION"
  echo "  Created: $OIDC_ARN"
fi

# ---------------------------------------------------------------------------
# Step 2: Create the IAM role (skip if it already exists)
# ---------------------------------------------------------------------------
echo ""
echo "Checking IAM role: $ROLE_NAME..."
if aws iam get-role --role-name "$ROLE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "  Role already exists: $ROLE_NAME (skipping creation)"
else
  echo "  Creating role..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$TRUST_POLICY_FILE" \
    --description "Assumed by GitHub Actions via OIDC to deploy Leonard to EC2" \
    --region "$REGION"
  echo "  Created role: $ROLE_NAME"
fi

# ---------------------------------------------------------------------------
# Step 3: Attach the inline permissions policy (put-role-policy is idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "Attaching inline permissions policy: $POLICY_NAME..."
EXISTING=$(aws iam get-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --region "$REGION" \
  --query PolicyName --output text 2>/dev/null || true)

if [ "$EXISTING" = "$POLICY_NAME" ]; then
  echo "  Policy already attached (skipping)"
else
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://$PERMISSION_POLICY_FILE" \
    --region "$REGION"
  echo "  Attached policy: $POLICY_NAME"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "Bootstrap complete."
echo ""
echo "Role ARN: arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Next steps:"
echo "  1. Add the role ARN to GitHub secrets as AWS_DEPLOY_ROLE_ARN"
echo "  2. Run Part B2 to create the SSM document: scripts/bootstrap-ssm-doc.sh"
