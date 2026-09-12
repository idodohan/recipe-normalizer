#!/bin/bash
set -e

# Add flyctl to PATH
export PATH="/Users/idodohan/.fly/bin:$PATH"

# Configuration
SUFFIX=$RANDOM
API_APP="rn-api-idodohan-$SUFFIX"
WEB_APP="rn-web-idodohan-$SUFFIX"
DB_APP="rn-db-idodohan-$SUFFIX"
REGION="iad" # Using a default region (Ashburn, Virginia)
API_KEY="${OPENROUTER_API_KEY:-}"

echo "========================================="
echo "Deploying Recipe Normalizer to Fly.io"
echo "API App: $API_APP"
echo "Web App: $WEB_APP"
echo "Database: $DB_APP"
echo "Region: $REGION"
echo "========================================="

echo "1. Creating Apps..."
fly apps create $API_APP --org personal
fly apps create $WEB_APP --org personal

echo "2. Setting up Database..."
fly postgres create --name $DB_APP --region $REGION --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1 --org personal
echo "Attaching DB to API app..."
fly postgres attach $DB_APP -a $API_APP -y

echo "3. Creating Persistent Volume..."
fly volumes create filestore -a $API_APP --region $REGION --size 1 -y

echo "4. Setting Secrets..."
fly secrets set OPENROUTER_API_KEY="$API_KEY" RN_COOKIE_SECURE="true" -a $API_APP

echo "5. Creating API fly.toml..."
cat > fly-api.toml <<EOF
app = "$API_APP"
primary_region = "$REGION"

[build]
  dockerfile = "docker/api.Dockerfile"

[env]
  RN_FILE_STORE_ROOT = "/data/filestore"

[mounts]
  source = "filestore"
  destination = "/data/filestore"
  initial_size = "1gb"

[processes]
  web = "uv run uvicorn recipe_normalizer.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"
  worker = "uv run python -m recipe_normalizer.worker"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 1
  processes = ["web"]

  [http_service.concurrency]
    type = "requests"
    hard_limit = 250
    soft_limit = 200

[[http_service.checks]]
  grace_period = "45s"
  interval = "15s"
  method = "GET"
  path = "/api/health"
  timeout = "5s"

[[vm]]
  memory = "1gb"
  cpu_kind = "shared"
  cpus = 1
  swap_size_mb = 1024 # Required for Playwright chromium
EOF

echo "6. Creating Web fly.toml..."
cat > fly-web.toml <<EOF
app = "$WEB_APP"
primary_region = "$REGION"

[build]
  dockerfile = "docker/web.Dockerfile"

[http_service]
  internal_port = 80
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 1
EOF

echo "7. Configuring Nginx..."
# Backup original nginx.conf
cp docker/nginx.conf docker/nginx.conf.bak
# Replace api:8000 with the internal flycast address of the API
sed -i '' "s/http:\/\/api:8000/http:\/\/$API_APP.internal:8000/g" docker/nginx.conf

echo "8. Deploying API App..."
# Run migrations and seed DB before full start. We can use a release command.
cat >> fly-api.toml <<EOF

[deploy]
  release_command = "sh -c 'uv run alembic upgrade head && uv run python -m recipe_normalizer.catalog.seed_loader'"
EOF

fly deploy -c fly-api.toml -a $API_APP --ha=false

echo "9. Deploying Web App..."
fly deploy -c fly-web.toml -a $WEB_APP --ha=false

echo "10. Restoring Nginx config..."
mv docker/nginx.conf.bak docker/nginx.conf

echo "========================================="
echo "Deployment Complete!"
echo "Your app is available at: https://$WEB_APP.fly.dev"
echo "========================================="
