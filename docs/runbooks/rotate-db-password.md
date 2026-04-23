# Runbook: Rotate DB Password

**Cadence:** Every 90 days, plus immediately after any of:
- Suspected compromise
- Offboarding a maintainer with SSM write access
- A CloudTrail alert on unauthorized `ssm:GetParameter` or `ssm:PutParameter`

## Steps

### 1. Generate new password and update SSM
```bash
NEW_PW=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-32)
AWS_PROFILE=leonard aws ssm put-parameter \
  --name /leonard/prod/POSTGRES_PASSWORD \
  --type SecureString \
  --value "$NEW_PW" \
  --overwrite \
  --region us-east-1
unset NEW_PW
echo "[+] SSM parameter updated. Value not echoed."
```

### 2. Deploy
Trigger a deploy to apply the new password:
```bash
# Via GitHub Actions (preferred):
# Go to Actions → deploy → Run workflow → main

# Or manually on EC2:
cd /opt/leonard && git fetch && git reset --hard origin/main && scripts/deploy-prod.sh
```

`deploy-prod.sh` will:
1. Fetch the new password from SSM
2. Write `/run/leonard/env`
3. Run `ALTER ROLE mct2 PASSWORD` to sync the DB volume
4. Restart the backend with the new `DATABASE_URL`

### 3. Verify
```bash
curl -fsS https://api.98-89-219-217.nip.io/health
```
Expected: HTTP 200.

## Rollback

If the new password causes issues, restore the previous value using SSM version history:

```bash
# List recent versions
AWS_PROFILE=leonard aws ssm get-parameter-history \
  --name /leonard/prod/POSTGRES_PASSWORD \
  --region us-east-1 \
  --query 'Parameters[*].{Version:Version,LastModifiedDate:LastModifiedDate}' \
  --output table

# Roll back to a specific version
LEONARD_SSM_VERSION=<previous-version> scripts/fetch-prod-secrets.sh
# Then re-run reconcile and restart backend manually, or just run deploy-prod.sh
```

Or via full deploy with pinned version:
```bash
LEONARD_SSM_VERSION=<version> scripts/deploy-prod.sh
```

## Quarterly reminder

A GitHub Actions scheduled workflow opens a reminder issue every 90 days. When you see it, follow these steps and close the issue.
