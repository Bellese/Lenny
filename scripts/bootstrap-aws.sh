#!/bin/bash
# One-time AWS bootstrap for Leonard prod:
#   - Creates the /leonard/prod/POSTGRES_PASSWORD SSM SecureString parameter
#   - Creates IAM policy, role, and instance profile for EC2 SSM access
#   - Associates the instance profile with the prod EC2 instance
#   - Enforces IMDSv2 (http-tokens=required, hop-limit=1)
#
# Run once from a workstation that has the 'leonard' AWS profile configured:
#   AWS_PROFILE=leonard ./scripts/bootstrap-aws.sh
#
# All steps are idempotent — re-running skips resources that already exist.
# The instance does NOT need to be stopped; IAM changes take effect within seconds.

set -euo pipefail

export AWS_DEFAULT_REGION=us-east-1

ACCOUNT_ID="439475769170"
REGION="us-east-1"
INSTANCE_ID="i-0f00585639d2f3ef1"
SSM_PARAM="/leonard/prod/POSTGRES_PASSWORD"
POLICY_NAME="leonard-prod-ssm-read"
ROLE_NAME="leonard-ec2-prod"
PROFILE_NAME="leonard-ec2-prod"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo ""
echo "=== Leonard prod AWS bootstrap ==="
echo "Account:  $ACCOUNT_ID"
echo "Region:   $REGION"
echo "Instance: $INSTANCE_ID"
echo ""

# ---------------------------------------------------------------------------
# 1. SSM parameter — skip if exists, generate random 32-char password if new
# ---------------------------------------------------------------------------
echo "[+] Checking SSM parameter $SSM_PARAM ..."
if aws ssm get-parameter --name "$SSM_PARAM" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] SSM parameter already exists — skipping creation"
else
  echo "[+] Generating random POSTGRES_PASSWORD ..."
  POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)
  echo "[+] Creating SSM SecureString parameter ..."
  aws ssm put-parameter \
    --name "$SSM_PARAM" \
    --value "$POSTGRES_PASSWORD" \
    --type "SecureString" \
    --description "Leonard prod Postgres password" \
    --region "$REGION"
  echo "[✓] SSM parameter created"
  echo ""
  echo "  IMPORTANT: The generated POSTGRES_PASSWORD has been stored in SSM."
  echo "  It is NOT printed here. Retrieve it with:"
  echo "    AWS_PROFILE=leonard aws ssm get-parameter --name '$SSM_PARAM' --with-decryption --region $REGION"
  echo ""
fi

# ---------------------------------------------------------------------------
# 2. IAM policy — skip if exists
# ---------------------------------------------------------------------------
echo "[+] Checking IAM policy $POLICY_NAME ..."
if aws iam get-policy --policy-arn "$POLICY_ARN" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] IAM policy already exists — skipping creation"
else
  echo "[+] Creating IAM policy from iam/leonard-prod-ssm-read-policy.json ..."
  # Note: the kms:Decrypt Resource uses a wildcard because SSM SecureString
  # parameters encrypted with the AWS-managed key (aws/ssm) do not expose the
  # exact key ARN at policy-write time. The Sid 'DecryptSsmManagedKey' documents
  # this intent. For customer-managed keys, replace '*' with the specific key ARN.
  aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://$REPO_ROOT/iam/leonard-prod-ssm-read-policy.json" \
    --description "Minimum SSM read permissions for Leonard prod EC2 instance" \
    --region "$REGION"
  echo "[✓] IAM policy created: $POLICY_ARN"
fi

# ---------------------------------------------------------------------------
# 3. IAM role — skip if exists
# ---------------------------------------------------------------------------
echo "[+] Checking IAM role $ROLE_NAME ..."
if aws iam get-role --role-name "$ROLE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] IAM role already exists — skipping creation"
else
  echo "[+] Creating IAM role from iam/leonard-ec2-prod-trust-policy.json ..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$REPO_ROOT/iam/leonard-ec2-prod-trust-policy.json" \
    --description "EC2 instance profile role for Leonard prod (SSM access)" \
    --region "$REGION"
  echo "[✓] IAM role created: $ROLE_NAME"
fi

# ---------------------------------------------------------------------------
# 4. Attach policy to role — skip if already attached
# ---------------------------------------------------------------------------
echo "[+] Checking policy attachment on role $ROLE_NAME ..."
ATTACHED=$(aws iam list-attached-role-policies \
  --role-name "$ROLE_NAME" \
  --region "$REGION" \
  --query "AttachedPolicies[?PolicyArn=='${POLICY_ARN}'].PolicyArn" \
  --output text)
if [ -n "$ATTACHED" ]; then
  echo "[=] Policy already attached — skipping"
else
  echo "[+] Attaching policy to role ..."
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "$POLICY_ARN" \
    --region "$REGION"
  echo "[✓] Policy attached"
fi

# ---------------------------------------------------------------------------
# 5. Instance profile — skip if exists
# ---------------------------------------------------------------------------
echo "[+] Checking instance profile $PROFILE_NAME ..."
if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] Instance profile already exists — skipping creation"
else
  echo "[+] Creating instance profile ..."
  aws iam create-instance-profile \
    --instance-profile-name "$PROFILE_NAME" \
    --region "$REGION"
  echo "[✓] Instance profile created"
fi

# ---------------------------------------------------------------------------
# 6. Add role to instance profile — skip if already there
# ---------------------------------------------------------------------------
echo "[+] Checking role membership in instance profile $PROFILE_NAME ..."
PROFILE_ROLE=$(aws iam get-instance-profile \
  --instance-profile-name "$PROFILE_NAME" \
  --region "$REGION" \
  --query "InstanceProfile.Roles[?RoleName=='${ROLE_NAME}'].RoleName" \
  --output text)
if [ -n "$PROFILE_ROLE" ]; then
  echo "[=] Role already in instance profile — skipping"
else
  echo "[+] Adding role to instance profile ..."
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$PROFILE_NAME" \
    --role-name "$ROLE_NAME" \
    --region "$REGION"
  echo "[✓] Role added to instance profile"
  # IAM propagation: EC2 needs ~10s before the profile is addressable for association
  echo "[+] Waiting 10s for IAM propagation before associating with EC2 ..."
  sleep 10
fi

# ---------------------------------------------------------------------------
# 7. Associate instance profile with EC2 instance
#    If a profile is already associated, disassociate first (AWS requirement)
# ---------------------------------------------------------------------------
echo "[+] Checking current instance profile association on $INSTANCE_ID ..."
ASSOC_ID=$(aws ec2 describe-iam-instance-profile-associations \
  --filters "Name=instance-id,Values=${INSTANCE_ID}" \
  --region "$REGION" \
  --query "IamInstanceProfileAssociations[?State=='associated' || State=='associating'].AssociationId" \
  --output text)

if [ -n "$ASSOC_ID" ]; then
  # Check if it's already our profile
  CURRENT_PROFILE=$(aws ec2 describe-iam-instance-profile-associations \
    --filters "Name=instance-id,Values=${INSTANCE_ID}" \
    --region "$REGION" \
    --query "IamInstanceProfileAssociations[?State=='associated' || State=='associating'].IamInstanceProfile.Arn" \
    --output text)
  EXPECTED_PROFILE_ARN="arn:aws:iam::${ACCOUNT_ID}:instance-profile/${PROFILE_NAME}"
  if [ "$CURRENT_PROFILE" = "$EXPECTED_PROFILE_ARN" ]; then
    echo "[=] Correct instance profile already associated — skipping"
  else
    echo "[+] Disassociating existing profile (${CURRENT_PROFILE}) ..."
    aws ec2 disassociate-iam-instance-profile \
      --association-id "$ASSOC_ID" \
      --region "$REGION"
    echo "[+] Waiting for disassociation to complete ..."
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION" 2>/dev/null || true
    sleep 5
    echo "[+] Associating new profile ..."
    aws ec2 associate-iam-instance-profile \
      --instance-id "$INSTANCE_ID" \
      --iam-instance-profile "Name=${PROFILE_NAME}" \
      --region "$REGION"
    echo "[✓] Instance profile swapped"
  fi
else
  echo "[+] Associating instance profile with EC2 instance ..."
  aws ec2 associate-iam-instance-profile \
    --instance-id "$INSTANCE_ID" \
    --iam-instance-profile "Name=${PROFILE_NAME}" \
    --region "$REGION"
  echo "[✓] Instance profile associated"
fi

# ---------------------------------------------------------------------------
# 8. Enforce IMDSv2 (security best practice: prevents SSRF-based metadata theft)
# ---------------------------------------------------------------------------
echo "[+] Enforcing IMDSv2 (http-tokens=required, hop-limit=1) ..."
aws ec2 modify-instance-metadata-options \
  --instance-id "$INSTANCE_ID" \
  --http-tokens required \
  --http-put-response-hop-limit 1 \
  --region "$REGION"
echo "[✓] IMDSv2 enforced"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Resources verified/created:"
echo "  SSM parameter : $SSM_PARAM"
echo "  IAM policy    : $POLICY_ARN"
echo "  IAM role      : $ROLE_NAME"
echo "  Instance profile: $PROFILE_NAME"
echo "  EC2 association : $INSTANCE_ID -> $PROFILE_NAME"
echo "  IMDSv2          : required (hop-limit=1)"
echo ""
echo "Next step: deploy fetch-prod-secrets.sh to the EC2 instance so it can"
echo "read secrets using the instance profile credentials (no static keys needed)."
