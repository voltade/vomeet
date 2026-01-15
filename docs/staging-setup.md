# Staging Environment Setup

This document describes how to set up and maintain the staging environment for Vomeet.

## Overview

The staging environment is a separate Kubernetes cluster for testing that includes:
- All microservices (API Gateway, Bot Manager, Admin API, Google Integration, Transcription Collector)
- CloudNativePG (PostgreSQL) database with fresh schema
- Dragonfly (Redis alternative)
- Kubernetes Jobs for bot instances

**Domain:** `https://vomeet.voltade.sg`

**Note:** Staging and production use separate clusters, so both use the `vomeet` namespace within their respective clusters. Staging uses a fresh database with the same schema as production, but no production data is synced.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Staging Environment                   │
│              (vomeet namespace - staging cluster)       │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  API Gateway │  │  Bot Manager │  │   Admin API   │  │
│  │   (staging)  │  │   (staging)  │  │   (staging)   │  │
│  └──────┬───────┘  └───────┬──────┘  └───────┬───────┘  │
│         │                  │                 │          │
│         └──────────────────┴─────────────────┘          │
│                            │                            │
│         ┌──────────────────┴──────────────────┐         │
│         │                                     │         │
│  ┌──────▼───────┐                    ┌────────▼──────┐  │
│  │   CNPG (PG)  │                    │   Dragonfly   │  │
│  │ (1 instance) │                    │    (Redis)    │  │
│  └──────────────┘                    └───────────────┘  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## GitHub Secrets & Variables Setup

### Required Secrets

Create these secrets in your GitHub repository (Settings → Secrets and variables → Actions):

#### Staging Environment Secrets

```bash
# Kubernetes Access (Staging)
KUBE_TOKEN_STAGING=<rancher-token-for-staging>
KUBE_CERTIFICATE_STAGING=<base64-encoded-ca-cert>

# Database (Staging)
DB_PASSWORD_STAGING=<random-secure-password>

# Admin API (Staging)
ADMIN_API_TOKEN_STAGING=<random-api-token>

# Notifications
TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
TELEGRAM_CHAT_ID=<your-telegram-chat-id>
TELEGRAM_THREAD_ID=<optional-thread-id>
```



### Required Variables

Create these variables in your GitHub repository:

```bash
# Staging Cluster
KUBE_CLUSTER_NAME_STAGING=local
KUBE_CLUSTER_URL_STAGING=https://rancher.voltade.sg/k8s/clusters/local
```

### How to Get Rancher Token (Service Account - Recommended for CI/CD)

For long-lived CI/CD access, create a Kubernetes service account instead of using personal tokens:

#### 1. Create Service Account

```bash
# Switch to vomeet namespace
kubectl config set-context --current --namespace=vomeet

# List existing service accounts (optional - check if it already exists)
kubectl get serviceaccounts -n vomeet

# Create service account
kubectl create serviceaccount vomeet-github-deployer -n vomeet

# Create a secret for the service account token (Kubernetes 1.24+)
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: vomeet-github-deployer-token
  namespace: vomeet
  annotations:
    kubernetes.io/service-account.name: vomeet-github-deployer
type: kubernetes.io/service-account-token
EOF
```

#### 2. Create ClusterRole with Required Permissions

```bash
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: vomeet-deployer
rules:
# Namespace management
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list", "create"]
# Deployments, ReplicaSets, Pods
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list", "watch"]
# Services and Endpoints
- apiGroups: [""]
  resources: ["services", "endpoints"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# ConfigMaps and Secrets
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# Jobs and CronJobs
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# Ingress and HTTPRoutes (Gateway API)
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["gateway.networking.k8s.io"]
  resources: ["httproutes", "gateways"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# CloudNativePG (CNPG) Clusters
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["clusters", "poolers", "backups", "scheduledbackups"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# PersistentVolumeClaims
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# ServiceAccounts
- apiGroups: [""]
  resources: ["serviceaccounts"]
  verbs: ["get", "list", "watch", "create", "update", "patch"]
# RBAC
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# Events (for debugging)
- apiGroups: [""]
  resources: ["events"]
  verbs: ["get", "list", "watch"]
EOF
```

#### 3. Bind Role to Service Account

```bash
# Bind for vomeet namespace (in staging cluster)
kubectl create clusterrolebinding vomeet-github-deployer \
  --clusterrole=vomeet-deployer \
  --serviceaccount=vomeet:vomeet-github-deployer
```

#### 4. Extract Token for GitHub Secrets

```bash
# Get the token (Kubernetes 1.24+)
TOKEN=$(kubectl get secret vomeet-github-deployer-token -n vomeet -o jsonpath='{.data.token}' | base64 -d)
echo $TOKEN

# Get the CA certificate
CA_CERT=$(kubectl get secret vomeet-github-deployer-token -n vomeet -o jsonpath='{.data.ca\.crt}')
echo $CA_CERT

# Get cluster server URL
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
echo $SERVER
```

#### 5. Add to GitHub Secrets

Add these values to your GitHub repository secrets:
- **KUBE_TOKEN_STAGING**: Use the `$TOKEN` value from above
- **KUBE_CERTIFICATE_STAGING**: Use the `$CA_CERT` value (already base64 encoded)
- **KUBE_CLUSTER_URL_STAGING**: Use the `$SERVER` value

#### 6. Test the Service Account

```bash
# Create a test kubeconfig
cat > /tmp/test-sa-kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- name: staging
  cluster:
    server: $SERVER
    certificate-authority-data: $CA_CERT
contexts:
- name: staging
  context:
    cluster: staging
    namespace: vomeet
    user: vomeet-github-deployer
users:
- name: vomeet-github-deployer
  user:
    token: $TOKEN
current-context: staging
EOF

# Test it
kubectl --kubeconfig=/tmp/test-sa-kubeconfig.yaml get pods -n vomeet

# Clean up test file
rm /tmp/test-sa-kubeconfig.yaml
```

### Alternative: Using Rancher User Token (Less Secure)

If you need quick access and can't create service accounts:

1. Log in to Rancher: https://rancher.voltade.com
2. Click on your user icon (top right) → "Account & API Keys"
3. Click "Create API Key"
4. Name it `vomeet-github-actions-staging`
5. Scope: Select the cluster you want to access
6. Set expiration: **Never** (for long-lived access) or **1 year**
7. Click "Create"
8. Copy the token (starts with `kubeconfig-user-...`)
9. Add to GitHub Secrets as `KUBE_TOKEN_STAGING`

**Note:** Service account tokens are preferred because:
- They don't expire
- They have minimal, scoped permissions
- They're not tied to a specific user account
- They can be easily revoked without affecting other services

### How to Get Certificate Authority Data

```bash
# From your existing kubeconfig
cat ~/.kube/config | grep certificate-authority-data | awk '{print $2}'

# OR extract from Rancher downloaded kubeconfig
# The certificate is already base64 encoded in the kubeconfig
```

## Workflows

### 1. Build Services (Staging)

**File:** `.github/workflows/build-services-staging.yaml`

**Triggers:**
- Push to `staging` branch
- Manual workflow dispatch

**What it does:**
- Detects which services changed
- Builds Docker images for changed services
- Tags images with `staging` and `staging-<git-sha>`
- Pushes to GitHub Container Registry (ghcr.io)

**Usage:**
```bash
# Automatic: Push to staging branch
git checkout staging
git merge main
git push origin staging

# Manual: Trigger via GitHub Actions UI
# Select "Build Services (Staging)" → "Run workflow"
# Choose services: all / specific services
```

### 2. Deploy Services (Staging)

**File:** `.github/workflows/deploy-services-staging.yaml`

**Triggers:**
- Push to `staging` branch (after build completes)
- Manual workflow dispatch

**What it does:**
- Checks if all required images exist
- Deploys Helm chart with staging values
- Runs database migrations
- Restarts all deployments
- Verifies deployment status

**Usage:**
```bash
# Automatic: Runs after successful build on staging branch

# Manual: Deploy specific tag
# GitHub Actions → "Deploy Services (Staging)" → "Run workflow"
# Enter image tag (default: staging)
```



## Initial Staging Setup

### Step 1: Create Kubernetes Namespace

```bash
kubectl create namespace vomeet
```

### Step 2: Set Up GitHub Secrets & Variables

Follow the "GitHub Secrets & Variables Setup" section above.

### Step 3: Initial Deployment

```bash
# Option A: Deploy from main branch
git checkout -b staging
git push origin staging
# This triggers build and deploy workflows

# Option B: Manual deployment
# Go to GitHub Actions → "Deploy Services (Staging)" → "Run workflow"
# Use tag: staging
```

### Step 4: Configure External Services

1. **Google OAuth Callback URL**
   - Go to Google Cloud Console → APIs & Services → Credentials
   - Add authorized redirect URI: `https://vomeet.voltade.sg/google/callback`

2. **Webhook URLs** (if using)
   - Update your external systems to point to staging webhook endpoint
   - Or disable webhooks in staging for testing

3. **Domain/DNS**
   - Ensure `vomeet.voltade.sg` points to your staging ingress/gateway

### Step 5: Verify Deployment

```bash
# Check all pods are running
kubectl get pods -n vomeet

# Check services
kubectl get svc -n vomeet

# Check database
kubectl get clusters.postgresql.cnpg.io -n vomeet

# Check logs
kubectl logs -n vomeet deployment/vomeet-api-gateway
```

## Configuration

### Bot Spawning for Scheduled Meetings

**Google Calendar Integration:** Bots are spawned **15 minutes** before a scheduled meeting starts

This is configured in the Helm chart value `google-integration.autoJoin.minutesBefore` (default: "15")

**How it works:**
1. Google Integration service polls for upcoming Calendar events
2. When a meeting is 15 minutes away, it calls bot-manager to spawn a bot
3. Bot joins immediately and waits in the meeting/waiting room
4. If meeting has `scheduledStartTime`, bot extends timeout until that time + 15 min buffer

**To change the spawn time:**
Update the Helm value:
```yaml
google-integration:
  autoJoin:
    minutesBefore: "15"  # Spawn bot X minutes before meeting
```

Or set via command line:
```bash
helm upgrade vomeet ./chart \
  --set google-integration.autoJoin.minutesBefore=15
```

## Maintenance

### Update Staging from Main

```bash
# Merge latest changes
git checkout staging
git merge main
git push origin staging
# This triggers automatic build and deploy

# Or rebase for cleaner history
git checkout staging
git rebase main
git push origin staging --force-with-lease
```

### Manual Rollback

```bash
# Rollback to previous version
helm rollback vomeet -n vomeet

# Or deploy specific version
gh workflow run deploy-services-staging.yaml \
  -f image_tag=staging-abc123def
```

### Scale Services

```bash
# Scale replicas
kubectl scale deployment vomeet-bot-manager -n vomeet --replicas=2

# Or update Helm values
helm upgrade vomeet ./chart \
  -n vomeet \
  -f chart/values-staging.yaml \
  --set bot-manager.replicaCount=2
```

### View Logs

```bash
# Stream logs from a service
kubectl logs -n vomeet -f deployment/vomeet-api-gateway

# View all pods logs
kubectl logs -n vomeet --all-containers=true --tail=100

# Follow specific pod
kubectl logs -n vomeet pod/vomeet-bot-manager-xxx-yyy -f
```

## Troubleshooting

### Pods CrashLooping

```bash
# Check pod status
kubectl get pods -n vomeet

# Describe pod to see events
kubectl describe pod <pod-name> -n vomeet

# Check logs
kubectl logs <pod-name> -n vomeet --previous
```

### Database Connection Issues

```bash
# Check CNPG cluster status
kubectl get clusters.postgresql.cnpg.io -n vomeet

# Check database pods
kubectl get pods -n vomeet -l cnpg.io/cluster=vomeet-cnpg

# Test connection
kubectl run -it --rm debug --image=postgres:17 --restart=Never -n vomeet -- \
  psql -h vomeet-cnpg-rw -U vomeet -d vomeet
```

### Migration Failures

```bash
# Check migration job logs
kubectl logs job/db-migrate -n vomeet

# Manually run migration
kubectl exec -it deployment/vomeet-transcription-collector -n vomeet -- \
  alembic -c /app/alembic.ini upgrade head

# Check current version
kubectl exec -it deployment/vomeet-transcription-collector -n vomeet -- \
  alembic -c /app/alembic.ini current
```

### Image Pull Errors

```bash
# Check image pull secrets
kubectl get secrets -n vomeet

# Manually pull to test
docker pull ghcr.io/voltade/vomeet-bot-manager:staging

# Re-authenticate with GHCR
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
```

## Differences from Production

| Feature | Production | Staging |
|---------|-----------|---------|
| Domain | vomeet.voltade.com | vomeet.voltade.sg |
| Image Tag | stable | staging |
| Database Instances | 3 (HA) | 1 |
| Service Replicas | 2-3 | 1 |
| Resources | High | Moderate |
| Auto-scaling | Enabled | Disabled |
| Backup Schedule | Daily | None |
| Test Data | Real user data | Generated test data |

## Cost Optimization

Staging uses fewer resources than production:
- **CPU:** ~30% of production
- **Memory:** ~40% of production
- **Storage:** ~20% of production
- **Database:** Single instance instead of HA cluster

To further reduce costs:
```bash
# Scale down during non-working hours
kubectl scale deployment --all --replicas=0 -n vomeet

# Scale up when needed
kubectl scale deployment --all --replicas=1 -n vomeet
```

## Security Considerations

1. **Secrets:** Use separate secrets for staging (don't reuse prod secrets)
2. **Access Control:** Limit who can deploy to staging namespace
3. **Network Policies:** Consider isolating staging network from production
4. **Test Data:** Use synthetic/generated data, never copy real user data
5. **Monitoring:** Set up alerts for unusual activity

## Next Steps

- [ ] Set up GitHub secrets for staging deployment
- [ ] Configure monitoring (Prometheus/Grafana for staging)
- [ ] Set up automated testing on staging before prod deploy
- [ ] Create test data generation scripts
- [ ] Set up cost alerts for staging environment
- [ ] Consider making `earlyJoinMinutes` configurable if needed
