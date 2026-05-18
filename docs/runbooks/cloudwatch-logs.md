# CloudWatch Logs — Lenny

## Log Groups

| Log Group | Service | Contents |
|---|---|---|
| `/leonard/caddy` | Reverse proxy | HTTP access logs (structured JSON): method, URI, status, duration, remote IP |
| `/leonard/backend` | FastAPI backend | Job submissions, FHIR queries, errors, request IDs |
| `/leonard/hapi-cdr` | HAPI FHIR CDR | FHIR interactions (bundle uploads, patient queries) |
| `/leonard/hapi-measure` | HAPI FHIR Measure Engine | CQL evaluation, measure runs |
| `/leonard/frontend` | React frontend (Nginx) | Static asset serving, 404s |

## One-Time Setup (apply IAM policy before first deploy)

The EC2 instance role `leonard-ec2-prod` must have the CloudWatch Logs write policy applied before the first deploy. Without this, containers using the `awslogs` driver fail to initialize and will not start.

```bash
export AWS_PROFILE=leonard
aws iam put-role-policy \
  --role-name leonard-ec2-prod \
  --policy-name CloudWatchLogsWrite \
  --policy-document file://iam/leonard-ec2-prod-cloudwatch-policy.json
```

Verify:
```bash
aws iam get-role-policy --role-name leonard-ec2-prod --policy-name CloudWatchLogsWrite
```

## Set Log Retention (run after first deploy)

AWS creates log groups with no retention ("Never Expire") by default. Set 90-day retention on all five groups:

```bash
export AWS_PROFILE=leonard
for group in /leonard/caddy /leonard/backend /leonard/hapi-cdr /leonard/hapi-measure /leonard/frontend; do
  aws logs put-retention-policy \
    --log-group-name "$group" \
    --retention-in-days 90
done
```

## Viewing Logs (replaces `docker logs`)

After deploying this PR, `docker logs <container>` will return "logging driver does not support reading". Use these instead:

### Tail latest events (last 30 minutes)

```bash
export AWS_PROFILE=leonard
# caddy access log
aws logs filter-log-events \
  --log-group-name /leonard/caddy \
  --start-time $(python3 -c "import time; print(int((time.time()-1800)*1000))") \
  --query 'events[].message' --output text

# backend
aws logs filter-log-events \
  --log-group-name /leonard/backend \
  --start-time $(python3 -c "import time; print(int((time.time()-1800)*1000))") \
  --query 'events[].message' --output text
```

### All five services

Replace `/leonard/caddy` with any of:
- `/leonard/backend`
- `/leonard/hapi-cdr`
- `/leonard/hapi-measure`
- `/leonard/frontend`

## CloudWatch Logs Insights Queries

Open [CloudWatch Logs Insights](https://us-east-1.console.aws.amazon.com/cloudwatch/home#logsV2:logs-insights) in the AWS console, select one or more log groups, and run these queries.

### Recent API traffic (excluding health checks)

```
fields @timestamp, request.method, request.uri, status, duration
| filter request.uri != "/health"
| sort @timestamp desc
| limit 100
```

**Log group:** `/leonard/caddy`

### Error rate by endpoint

```
fields request.uri, status
| filter status >= 400
| filter request.uri != "/health"
| stats count() as errors by request.uri, status
| sort errors desc
```

**Log group:** `/leonard/caddy`

### Job submissions this week

```
fields @timestamp, @message
| filter @message like /job/
| sort @timestamp desc
| limit 200
```

**Log group:** `/leonard/backend`

### Top remote IPs (Connectathon usage)

```
fields request.remote_ip
| stats count() as requests by request.remote_ip
| sort requests desc
| limit 20
```

**Log group:** `/leonard/caddy`

## PHI / Access Restriction Note

**Before Lenny processes real patient data**, log group access must be restricted. Real FHIR bundle payloads and query parameters may appear in HAPI logs. Currently the only IAM policy granting log access is `CloudWatchLogsWrite` on the EC2 role — no read access is granted to developers.

When transitioning to real patient data:
1. Confirm log content does not include PHI (review a sample from `/leonard/hapi-cdr`)
2. If PHI is present, evaluate whether HAPI log level can be reduced to `WARN` via `JAVA_TOOL_OPTIONS=-Dlogging.level.root=WARN`
3. Restrict CloudWatch Logs Insights access to authorized users via IAM or resource-based policy
