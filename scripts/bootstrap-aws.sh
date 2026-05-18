#!/bin/bash
# One-time AWS bootstrap for Leonard prod:
#   - Creates the /leonard/prod/POSTGRES_PASSWORD SSM SecureString parameter
#   - Creates the /leonard/prod/CDR_FERNET_KEY SSM SecureString parameter
#   - Creates IAM policy, role, and instance profile for EC2 SSM access
#   - Applies inline CloudWatch Logs write policy to leonard-ec2-prod role
#   - Pre-creates CloudWatch log groups with 90-day retention
#   - Creates SNS topic (leonard-alerts) and email subscription for alerts
#   - Creates CloudWatch Logs metric filters for Caddy 5xx and backend errors
#   - Creates CloudWatch Alarms that fire to leonard-alerts SNS topic
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
SSM_FERNET_PARAM="/leonard/prod/CDR_FERNET_KEY"
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
# 0. Verify active AWS profile resolves to the expected account
# ---------------------------------------------------------------------------
echo "[+] Verifying AWS account..."
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
if [ "$CALLER_ACCOUNT" != "$ACCOUNT_ID" ]; then
  echo "[!] ERROR: Active AWS profile resolves to account $CALLER_ACCOUNT, expected $ACCOUNT_ID. Aborting."
  exit 1
fi
echo "[+] Confirmed account: $CALLER_ACCOUNT"

# ---------------------------------------------------------------------------
# 1. SSM parameter — skip if exists, generate random 32-char password if new
# ---------------------------------------------------------------------------
echo "[+] Checking SSM parameter $SSM_PARAM ..."
if aws ssm get-parameter --name "$SSM_PARAM" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] SSM parameter already exists — skipping creation"
else
  echo "[+] Generating random POSTGRES_PASSWORD ..."
  # Write param JSON to a mode-600 temp file so the password never appears as a
  # process argument (visible via `ps` / /proc/<pid>/cmdline).
  PW_JSON=$(mktemp)
  chmod 600 "$PW_JSON"
  python3 -c "
import json, sys
print(json.dumps({
  'Name': sys.argv[1],
  'Value': sys.argv[2],
  'Type': 'SecureString',
  'Description': 'Leonard prod Postgres password',
  'Overwrite': False,
  'Tier': 'Standard'
}))
" "$SSM_PARAM" \
  "$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-32)" > "$PW_JSON"
  echo "[+] Creating SSM SecureString parameter ..."
  aws ssm put-parameter --cli-input-json "file://$PW_JSON" --region "$REGION"
  rm -f "$PW_JSON"
  echo "[+] SSM parameter $SSM_PARAM created (value stored in SSM, not printed)"
  echo ""
  echo "  IMPORTANT: The generated POSTGRES_PASSWORD has been stored in SSM."
  echo "  It is NOT printed here. Retrieve it with:"
  echo "    AWS_PROFILE=leonard aws ssm get-parameter --name '$SSM_PARAM' --with-decryption --region $REGION"
  echo ""
fi

# ---------------------------------------------------------------------------
# 1b. CDR_FERNET_KEY SSM parameter — skip if exists, generate Fernet key if new
# ---------------------------------------------------------------------------
echo "[+] Checking SSM parameter $SSM_FERNET_PARAM ..."
if aws ssm get-parameter --name "$SSM_FERNET_PARAM" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] SSM parameter already exists — skipping creation"
else
  echo "[+] Generating Fernet key for CDR credential encryption ..."
  # Write param JSON to a mode-600 temp file so the key never appears as a
  # process argument (visible via `ps` / /proc/<pid>/cmdline).
  FERNET_JSON=$(mktemp)
  chmod 600 "$FERNET_JSON"
  python3 -c "
import json, sys
from cryptography.fernet import Fernet
print(json.dumps({
  'Name': sys.argv[1],
  'Value': Fernet.generate_key().decode(),
  'Type': 'SecureString',
  'Description': 'Leonard prod Fernet key for CDR credential encryption at rest',
  'Overwrite': False,
  'Tier': 'Standard'
}))
" "$SSM_FERNET_PARAM" > "$FERNET_JSON"
  echo "[+] Creating SSM SecureString parameter ..."
  aws ssm put-parameter --cli-input-json "file://$FERNET_JSON" --region "$REGION"
  rm -f "$FERNET_JSON"
  echo "[+] SSM parameter $SSM_FERNET_PARAM created (value stored in SSM, not printed)"
  echo ""
  echo "  IMPORTANT: The generated CDR_FERNET_KEY has been stored in SSM."
  echo "  It is NOT printed here. Retrieve it with:"
  echo "    AWS_PROFILE=leonard aws ssm get-parameter --name '$SSM_FERNET_PARAM' --with-decryption --region $REGION"
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
# 4b. Attach AmazonSSMManagedInstanceCore — required for SSM Run Command
# ---------------------------------------------------------------------------
SSM_CORE_ARN="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
echo "[+] Checking AmazonSSMManagedInstanceCore attachment ..."
ATTACHED_CORE=$(aws iam list-attached-role-policies \
  --role-name "$ROLE_NAME" \
  --region "$REGION" \
  --query "AttachedPolicies[?PolicyArn=='${SSM_CORE_ARN}'].PolicyArn" \
  --output text)
if [ -n "$ATTACHED_CORE" ]; then
  echo "[=] AmazonSSMManagedInstanceCore already attached — skipping"
else
  echo "[+] Attaching AmazonSSMManagedInstanceCore ..."
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "$SSM_CORE_ARN" \
    --region "$REGION"
  echo "[✓] AmazonSSMManagedInstanceCore attached"
fi

# ---------------------------------------------------------------------------
# 4c. Inline CloudWatch Logs write policy — required for awslogs driver
#     Without this, containers using awslogs fail to initialize and won't start.
# ---------------------------------------------------------------------------
CW_POLICY_NAME="CloudWatchLogsWrite"
echo "[+] Checking inline policy $CW_POLICY_NAME on role $ROLE_NAME ..."
if aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$CW_POLICY_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] Inline policy $CW_POLICY_NAME already exists — skipping"
else
  echo "[+] Applying inline CloudWatch Logs write policy ..."
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$CW_POLICY_NAME" \
    --policy-document "file://$REPO_ROOT/iam/leonard-ec2-prod-cloudwatch-policy.json" \
    --region "$REGION"
  echo "[✓] Inline policy $CW_POLICY_NAME applied"
fi

# ---------------------------------------------------------------------------
# 4d. Pre-create CloudWatch log groups with 90-day retention
#     awslogs-create-group:true creates groups on container start with no
#     retention (Never Expire). Pre-creating here guarantees retention is set
#     before any logs arrive, even on DR rebuilds.
# ---------------------------------------------------------------------------
LOG_GROUPS=("/leonard/caddy" "/leonard/backend" "/leonard/hapi-cdr" "/leonard/hapi-measure" "/leonard/frontend")
for LG in "${LOG_GROUPS[@]}"; do
  echo "[+] Checking log group $LG ..."
  if aws logs describe-log-groups --log-group-name-prefix "$LG" --region "$REGION" \
      --query "logGroups[?logGroupName=='$LG'].logGroupName" --output text 2>/dev/null | grep -q "$LG"; then
    echo "[=] Log group $LG already exists — ensuring 90-day retention ..."
  else
    echo "[+] Creating log group $LG ..."
    aws logs create-log-group --log-group-name "$LG" --region "$REGION"
    echo "[✓] Log group $LG created"
  fi
  aws logs put-retention-policy --log-group-name "$LG" --retention-in-days 90 --region "$REGION"
  echo "[✓] Retention set to 90 days on $LG"
done

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
# 9. SNS topic for alerts
# ---------------------------------------------------------------------------
ALERT_EMAIL="msutton@bellese.io"
SNS_TOPIC_NAME="leonard-alerts"

echo "[+] Checking SNS topic $SNS_TOPIC_NAME ..."
SNS_TOPIC_ARN=$(aws sns list-topics --region "$REGION" \
  --query "Topics[?ends_with(TopicArn, ':$SNS_TOPIC_NAME')].TopicArn" \
  --output text 2>/dev/null)
if [ -n "$SNS_TOPIC_ARN" ]; then
  echo "[=] SNS topic already exists: $SNS_TOPIC_ARN"
else
  SNS_TOPIC_ARN=$(aws sns create-topic --name "$SNS_TOPIC_NAME" --region "$REGION" \
    --query TopicArn --output text)
  echo "[✓] SNS topic created: $SNS_TOPIC_ARN"
fi

echo "[+] Checking email subscription for $ALERT_EMAIL ..."
EXISTING_SUB=$(aws sns list-subscriptions-by-topic --topic-arn "$SNS_TOPIC_ARN" \
  --region "$REGION" \
  --query "Subscriptions[?Endpoint=='$ALERT_EMAIL'].SubscriptionArn" \
  --output text 2>/dev/null)
if [ -n "$EXISTING_SUB" ]; then
  echo "[=] Email subscription already exists — skipping"
else
  aws sns subscribe \
    --topic-arn "$SNS_TOPIC_ARN" \
    --protocol email \
    --notification-endpoint "$ALERT_EMAIL" \
    --region "$REGION"
  echo "[✓] Subscription confirmation sent to $ALERT_EMAIL"
  echo "    *** Check your inbox and confirm the subscription or alerts will not deliver ***"
fi

# ---------------------------------------------------------------------------
# 10. CloudWatch Logs metric filter: Caddy 5xx errors
#     Caddy v2 JSON access logs have 'status' as a top-level integer field.
# ---------------------------------------------------------------------------
FILTER_5XX_NAME="caddy-5xx-errors"
echo "[+] Checking metric filter $FILTER_5XX_NAME ..."
EXISTING_5XX=$(aws logs describe-metric-filters \
  --log-group-name /leonard/caddy \
  --filter-name-prefix "$FILTER_5XX_NAME" \
  --region "$REGION" \
  --query "metricFilters[?filterName=='$FILTER_5XX_NAME'].filterName" \
  --output text 2>/dev/null)
if [ -n "$EXISTING_5XX" ]; then
  echo "[=] Metric filter $FILTER_5XX_NAME already exists — skipping"
else
  aws logs put-metric-filter \
    --log-group-name /leonard/caddy \
    --filter-name "$FILTER_5XX_NAME" \
    --filter-pattern '{ $.status >= 500 }' \
    --metric-transformations \
      "metricName=Caddy5xxErrors,metricNamespace=Leonard,metricValue=1,defaultValue=0" \
    --region "$REGION"
  echo "[✓] Metric filter $FILTER_5XX_NAME created"
fi

# ---------------------------------------------------------------------------
# 11. CloudWatch Logs metric filter: Backend ERROR-level logs
#     Backend JSON formatter writes "level": record.levelname (uppercase).
# ---------------------------------------------------------------------------
FILTER_BACKEND_ERROR_NAME="backend-error-logs"
echo "[+] Checking metric filter $FILTER_BACKEND_ERROR_NAME ..."
EXISTING_BE=$(aws logs describe-metric-filters \
  --log-group-name /leonard/backend \
  --filter-name-prefix "$FILTER_BACKEND_ERROR_NAME" \
  --region "$REGION" \
  --query "metricFilters[?filterName=='$FILTER_BACKEND_ERROR_NAME'].filterName" \
  --output text 2>/dev/null)
if [ -n "$EXISTING_BE" ]; then
  echo "[=] Metric filter $FILTER_BACKEND_ERROR_NAME already exists — skipping"
else
  aws logs put-metric-filter \
    --log-group-name /leonard/backend \
    --filter-name "$FILTER_BACKEND_ERROR_NAME" \
    --filter-pattern '{ $.level = "ERROR" }' \
    --metric-transformations \
      "metricName=BackendErrors,metricNamespace=Leonard,metricValue=1,defaultValue=0" \
    --region "$REGION"
  echo "[✓] Metric filter $FILTER_BACKEND_ERROR_NAME created"
fi

# ---------------------------------------------------------------------------
# 12. CloudWatch Alarm: Caddy 5xx — >5 errors in any 5-minute window
# ---------------------------------------------------------------------------
ALARM_5XX_NAME="leonard-caddy-5xx"
echo "[+] Checking alarm $ALARM_5XX_NAME ..."
EXISTING_A5=$(aws cloudwatch describe-alarms \
  --alarm-names "$ALARM_5XX_NAME" \
  --region "$REGION" \
  --query "MetricAlarms[0].AlarmName" \
  --output text 2>/dev/null)
if [ "$EXISTING_A5" = "$ALARM_5XX_NAME" ]; then
  echo "[=] Alarm $ALARM_5XX_NAME already exists — skipping"
else
  aws cloudwatch put-metric-alarm \
    --alarm-name "$ALARM_5XX_NAME" \
    --alarm-description "Caddy: >5 HTTP 5xx responses in a 5-minute window" \
    --metric-name Caddy5xxErrors \
    --namespace Leonard \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --threshold 5 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$SNS_TOPIC_ARN" \
    --ok-actions "$SNS_TOPIC_ARN" \
    --region "$REGION"
  echo "[✓] Alarm $ALARM_5XX_NAME created"
fi

# ---------------------------------------------------------------------------
# 13. CloudWatch Alarm: Backend errors — any ERROR log in a 5-minute window
# ---------------------------------------------------------------------------
ALARM_BACKEND_NAME="leonard-backend-errors"
echo "[+] Checking alarm $ALARM_BACKEND_NAME ..."
EXISTING_ABE=$(aws cloudwatch describe-alarms \
  --alarm-names "$ALARM_BACKEND_NAME" \
  --region "$REGION" \
  --query "MetricAlarms[0].AlarmName" \
  --output text 2>/dev/null)
if [ "$EXISTING_ABE" = "$ALARM_BACKEND_NAME" ]; then
  echo "[=] Alarm $ALARM_BACKEND_NAME already exists — skipping"
else
  aws cloudwatch put-metric-alarm \
    --alarm-name "$ALARM_BACKEND_NAME" \
    --alarm-description "Backend: 1+ ERROR-level log events in a 5-minute window" \
    --metric-name BackendErrors \
    --namespace Leonard \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --threshold 0 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$SNS_TOPIC_ARN" \
    --ok-actions "$SNS_TOPIC_ARN" \
    --region "$REGION"
  echo "[✓] Alarm $ALARM_BACKEND_NAME created"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Resources verified/created:"
echo "  SSM parameter    : $SSM_PARAM"
echo "  IAM policy       : $POLICY_ARN"
echo "  IAM role         : $ROLE_NAME"
echo "  CW inline policy : $CW_POLICY_NAME on $ROLE_NAME"
echo "  CW log groups    : /leonard/{caddy,backend,hapi-cdr,hapi-measure,frontend} (90d retention)"
echo "  SNS topic        : $SNS_TOPIC_ARN"
echo "  CW metric filters: $FILTER_5XX_NAME, $FILTER_BACKEND_ERROR_NAME"
echo "  CW alarms        : $ALARM_5XX_NAME, $ALARM_BACKEND_NAME"
echo "  Instance profile : $PROFILE_NAME"
echo "  EC2 association  : $INSTANCE_ID -> $PROFILE_NAME"
echo "  IMDSv2           : required (hop-limit=1)"
echo ""
echo "Next step: deploy fetch-prod-secrets.sh to the EC2 instance so it can"
echo "read secrets using the instance profile credentials (no static keys needed)."
