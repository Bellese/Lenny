# Deployment: EC2 Hosting + CI/CD Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four infrastructure files that give Leonard a cloud-hosted URL with automatic HTTPS and a GitHub Actions deploy pipeline gated on passing tests.

**Architecture:** A compose overlay (`docker-compose.prod.yml`) adds Caddy SSL termination and a frontend build arg to the existing stack. The frontend API URL is baked in at Docker build time via a `Dockerfile` ARG; locally the default (`localhost:8000`) applies unchanged. GitHub Actions SSHes into EC2 on every push to `master`, running backend unit tests and a frontend build check before deploying.

**Tech Stack:** Docker Compose v2 plugin, Caddy 2 Alpine, GitHub Actions (`appleboy/ssh-action@v1`), nip.io

---

### Task 1: Frontend Dockerfile — inject API URL at build time

**Files:**
- Modify: `frontend/Dockerfile`

- [ ] **Step 1: Add build arg to Dockerfile**

Replace the full contents of `frontend/Dockerfile` with:

```dockerfile
# Build stage
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
ARG REACT_APP_API_URL=http://localhost:8000
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN npm run build

# Production stage
FROM node:20-alpine
WORKDIR /app
RUN npm install -g serve@14
COPY --from=build /app/build ./build
EXPOSE 3001
CMD ["serve", "-s", "build", "-l", "3001"]
```

The `ARG` makes the value available during `npm run build`. The `ENV` line copies it so CRA picks it up at build time. The default preserves existing local behavior.

- [ ] **Step 2: Build without arg — verify localhost:8000 is embedded**

```bash
docker build -t leonard-frontend-test ./frontend
docker run --rm leonard-frontend-test grep -rl "localhost:8000" /app/build/static/js/
```

Expected: one or more `.js` filenames printed. If empty, the default arg is not being picked up.

- [ ] **Step 3: Build with arg override — verify custom URL is embedded**

```bash
docker build --build-arg REACT_APP_API_URL=https://api.test-verify.nip.io \
  -t leonard-frontend-arg ./frontend
docker run --rm leonard-frontend-arg grep -rl "test-verify.nip.io" /app/build/static/js/
```

Expected: one or more `.js` filenames printed containing `test-verify.nip.io`.

- [ ] **Step 4: Clean up test images**

```bash
docker rmi leonard-frontend-test leonard-frontend-arg
```

- [ ] **Step 5: Commit**

```bash
git add frontend/Dockerfile
git commit -m "feat: add REACT_APP_API_URL build arg to frontend Dockerfile"
```

---

### Task 2: Caddyfile — two virtual hosts

**Files:**
- Create: `Caddyfile` (repo root)

Note on syntax: Caddy uses `{$VAR}` to read environment variables (not `${VAR}` — that is Docker Compose syntax). The deploy script sets `CADDY_HOST=<ip>.nip.io` before starting Caddy.

- [ ] **Step 1: Create Caddyfile**

Create `Caddyfile` at the repo root:

```
{$CADDY_HOST} {
    reverse_proxy frontend:3001
}

api.{$CADDY_HOST} {
    reverse_proxy backend:8000
}
```

- [ ] **Step 2: Validate Caddyfile syntax**

```bash
docker run --rm \
  -e CADDY_HOST=54-12-34-56.nip.io \
  -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

Expected output includes `Valid configuration` with no errors.

- [ ] **Step 3: Commit**

```bash
git add Caddyfile
git commit -m "feat: add Caddyfile for SSL termination and reverse proxy"
```

---

### Task 3: docker-compose.prod.yml — production overlay

**Files:**
- Create: `docker-compose.prod.yml` (repo root)

Important: Docker Compose variable substitution supports `${VAR}` and `${VAR:-default}` only — it does NOT support bash string manipulation like `${VAR//./-}`. Use `${CADDY_HOST}` directly; the deploy script computes and exports it before running compose.

- [ ] **Step 1: Create docker-compose.prod.yml**

Create `docker-compose.prod.yml` at the repo root:

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

- [ ] **Step 2: Validate merged compose config**

```bash
CADDY_HOST=54-12-34-56.nip.io \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

Expected: merged YAML printed with no errors. Confirm:
- `frontend.build.args.REACT_APP_API_URL` equals `https://api.54-12-34-56.nip.io`
- `caddy` service is present with ports 80 and 443

- [ ] **Step 3: Commit**

```bash
git add docker-compose.prod.yml
git commit -m "feat: add docker-compose.prod.yml production overlay with Caddy"
```

---

### Task 4: GitHub Actions CI/CD workflow

**Files:**
- Create: `.github/workflows/deploy.yml`

- [ ] **Step 1: Create the workflows directory**

```bash
mkdir -p .github/workflows
```

- [ ] **Step 2: Create deploy.yml**

Create `.github/workflows/deploy.yml`:

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

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml')); print('YAML valid')"
```

Expected: `YAML valid`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat: add GitHub Actions CI/CD pipeline for EC2 deploy"
```

---

### Task 5: Fix spec — Docker Compose variable syntax

**Files:**
- Modify: `docs/superpowers/specs/2026-04-08-deployment-design.md`

The published spec uses `${EC2_PUBLIC_IP//./-}` in the compose file example, which Docker Compose does not support. Update it to match the implementation.

- [ ] **Step 1: Fix the compose snippet in the spec**

In `docs/superpowers/specs/2026-04-08-deployment-design.md`, find:

```
        REACT_APP_API_URL: https://api.${EC2_PUBLIC_IP//./-}.nip.io
```

Replace with:

```
        REACT_APP_API_URL: https://api.${CADDY_HOST}
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-08-deployment-design.md
git commit -m "docs: fix Docker Compose variable syntax in deployment spec"
```

---

## EC2 Provisioning Reference (manual, one-time)

Not automated — run these steps once before the first deploy. GitHub Actions takes over after that.

**Launch instance (AWS Console):**
- Amazon Linux 2023, t3.small, 30 GB gp3 EBS
- Assign Elastic IP immediately
- Security group: inbound 22 (SSH, your IP only), 80 (HTTP, 0.0.0.0/0), 443 (HTTPS, 0.0.0.0/0)

**Bootstrap (SSH into the instance):**

```bash
sudo dnf install -y docker docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# Log out and back in for group change to take effect

sudo mkdir -p /opt/leonard
sudo git clone https://github.com/Bellese/mct2.git /opt/leonard
sudo chown -R ec2-user:ec2-user /opt/leonard
```

**First deploy (run after bootstrap):**

```bash
cd /opt/leonard
export EC2_PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
export CADDY_HOST="${EC2_PUBLIC_IP//./-}.nip.io"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**Add GitHub secrets** (`EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`) — then all future deploys are automatic.
