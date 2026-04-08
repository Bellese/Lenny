# Deployment: EC2 Hosting + CI/CD Pipeline

**Issue:** [Bellese/mct2#4](https://github.com/Bellese/mct2/issues/4)
**Date:** 2026-04-08
**Status:** Approved

---

## Objective

Make Leonard accessible via a cloud-hosted URL so the Product Owner can review changes without installing Docker, and CI/CD ensures every merge to `master` is automatically tested and deployed.

## Out of scope

- Connectathon measure seeding (seed bundle content) — separate follow-on once PO confirms track measures (~May 1)
- ECS Fargate migration — deferred until HAPI H2 → PostgreSQL migration post-connectathon

---

## Architecture

Five existing services (frontend, backend, db, hapi-fhir-cdr, hapi-fhir-measure) are unchanged. Two things are added for production:

- **Caddy** — a 7th Docker service that terminates SSL and proxies traffic to frontend and backend
- **docker-compose.prod.yml** — a compose overlay applied only on EC2; never used locally

### URL structure

| Host | Routes to | Purpose |
|------|-----------|---------|
| `https://<ip>.nip.io` | frontend:3001 | React UI |
| `https://api.<ip>.nip.io` | backend:8000 | FastAPI |

Both subdomains resolve to the same Elastic IP. Caddy auto-fetches Let's Encrypt certs for both via nip.io. No IT/DNS involvement required.

`<ip>` format: dots replaced with dashes (e.g. `54.12.34.56` → `54-12-34-56.nip.io`).

### Why not Fargate

HAPI FHIR uses H2 file storage in Docker volumes. H2 warns against NFS-based filesystems (file locking issues, potential corruption). EC2 with local Docker volumes avoids this entirely. ECS Fargate is the right long-term target after H2 → PostgreSQL migration.

---

## Files to create / modify

### 1. `frontend/Dockerfile` (modify)

Add before `RUN npm run build`:

```dockerfile
ARG REACT_APP_API_URL=http://localhost:8000
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN npm run build
```

Default preserves existing local behavior. Production overrides via compose build arg.

### 2. `docker-compose.prod.yml` (new)

```yaml
services:
  frontend:
    build:
      context: ./frontend
      args:
        REACT_APP_API_URL: https://api.${CADDY_HOST}

  caddy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443", "443:443/udp"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - frontend
      - backend

volumes:
  caddy_data:
  caddy_config:
```

### 3. `Caddyfile` (new, repo root)

```
{$CADDY_HOST} {
    reverse_proxy frontend:3001
}

api.{$CADDY_HOST} {
    reverse_proxy backend:8000
}
```

`CADDY_HOST` is set in the EC2 shell environment before each deploy (see deploy script below).

### 4. `.github/workflows/deploy.yml` (new)

```yaml
name: Test and Deploy

on:
  push:
    branches: [master]

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Run backend unit tests
        working-directory: backend
        run: |
          pip install -r requirements.txt
          python -m pytest tests/ --ignore=tests/integration -v

      - name: Check frontend build
        working-directory: frontend
        run: |
          npm install
          npm run build

      - name: Deploy to EC2
        if: success()
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.EC2_HOST }}
          username: ${{ secrets.EC2_USER }}
          key: ${{ secrets.EC2_SSH_KEY }}
          script: |
            cd /opt/leonard
            git pull origin master
            export EC2_PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
            export CADDY_HOST="${EC2_PUBLIC_IP//./-}.nip.io"
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
            sleep 10 && curl -f https://api.${CADDY_HOST}/health || exit 1
            docker compose ps
```

**GitHub secrets required:**

| Secret | Value |
|--------|-------|
| `EC2_HOST` | Elastic IP address |
| `EC2_USER` | `ec2-user` |
| `EC2_SSH_KEY` | Private key of the EC2 key pair |

**Rollback:** SSH in and run:
```bash
cd /opt/leonard
git checkout HEAD~1
export EC2_PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
export CADDY_HOST="${EC2_PUBLIC_IP//./-}.nip.io"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

---

## EC2 provisioning (one-time manual setup)

**Launch instance:**
- Amazon Linux 2023, t3.small (2 vCPU / 2 GB RAM), 30 GB gp3 EBS
- Assign Elastic IP before sharing any URL
- Security group: inbound 22 (SSH, your IP only), 80 (HTTP, 0.0.0.0/0), 443 (HTTPS, 0.0.0.0/0)

**Bootstrap (run once via SSH):**
```bash
sudo dnf install -y docker docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user

sudo mkdir -p /opt/leonard
sudo git clone https://github.com/Bellese/mct2.git /opt/leonard
sudo chown -R ec2-user:ec2-user /opt/leonard
```

**First deploy (run manually after bootstrap):**
```bash
cd /opt/leonard
export EC2_PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
export CADDY_HOST="${EC2_PUBLIC_IP//./-}.nip.io"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

After this, every push to `master` triggers automated deploy via GitHub Actions.

---

## CI/CD pipeline steps

1. Backend unit tests (`pytest`, ignoring integration tests)
2. Frontend build check (`npm run build`)
3. SSH deploy to EC2 (only if both pass)
4. Post-deploy health check (`curl /health`)

Integration tests are deferred — they require the full Docker stack in CI and add significant complexity. Revisit post-connectathon.

---

## Data loss risk

If the EC2 instance is **terminated** (not just stopped), Docker volume data (HAPI H2 files, Postgres data) is lost. Acceptable for May. Mitigated post-connectathon by migrating HAPI to PostgreSQL with durable storage.
