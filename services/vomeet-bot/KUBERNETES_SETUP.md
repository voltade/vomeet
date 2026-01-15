# Kubernetes Setup for Vomeet Bot

This document describes how to set up vomeet-bot on Kubernetes, following the patterns established in the envoy-crm repository.

## Overview

The Kubernetes setup uses:
- **Helm charts** for infrastructure deployment
- **Kubernetes Jobs** for ephemeral bot instances (one per meeting)
- **External Secrets Operator** for secret management
- **ServiceAccounts and RBAC** for security
- **Dynamic Job creation** via bot-manager's Kubernetes orchestrator

## Architecture

```
┌─────────────────┐
│  bot-manager    │  Creates Kubernetes Jobs on-demand
│  (ORCHESTRATOR  │  via Kubernetes API
│   = k8s)        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Kubernetes     │
│  Job            │  vomeet-bot-{meeting_id}-{uuid}
│  (ephemeral)    │  Runs until meeting ends
└─────────────────┘
```

## Quick Start

### 1. Install Helm Chart

```bash
# Create namespace
kubectl create namespace vomeet-bot

# Install chart
helm install vomeet-bot ./chart \
  --namespace vomeet-bot \
  --set imageTag=production \
  --set imageRepository=ghcr.io/voltade/vomeet-bot
```

### 2. Configure External Secrets

Edit `chart/templates/secret-envs-from-onepassword.yaml` and `chart/templates/secret-envs-from-secretstore.yaml` to add your secret references.

### 3. Configure bot-manager

Set environment variables:

```bash
ORCHESTRATOR=k8s
K8S_NAMESPACE=vomeet-bot
K8S_SERVICE_ACCOUNT=vomeet-bot
K8S_BOT_IMAGE_REPOSITORY=git.voltade.com/voltade/vomeet-bot
K8S_BOT_IMAGE_TAG=production
REDIS_URL=redis://redis:6379/0
WHISPER_LIVE_URL=ws://whisperlive:8000/ws
```

### 4. Grant bot-manager RBAC permissions

Create a RoleBinding for bot-manager to create/delete Jobs:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: bot-manager
  namespace: vomeet-bot
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: bot-manager
  namespace: vomeet-bot
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: bot-manager
subjects:
  - kind: ServiceAccount
    name: bot-manager
    namespace: vomeet-bot
```

## File Structure

```
services/vomeet-bot/
├── chart/
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── values.staging.yaml
│   ├── README.md
│   └── templates/
│       ├── _helpers.tpl
│       ├── serviceaccount.yaml
│       ├── rbac.yaml
│       ├── clustersecretstore.yaml
│       ├── secret-envs-from-onepassword.yaml
│       ├── secret-envs-from-secretstore.yaml
│       ├── secret-registry.yaml
│       └── job-bot.yaml (reference template)
│
services/bot-manager/
└── app/orchestrators/
    ├── __init__.py (updated to support k8s)
    └── k8s.py (new Kubernetes orchestrator)
```

## Key Differences from envoy-crm

| Aspect | envoy-crm | vomeet-bot |
|--------|-----------|------------|
| **Workload Type** | Deployment (long-running) | Job (ephemeral) |
| **Replicas** | Fixed (3 prod, 2 staging) | Dynamic (created per meeting) |
| **Creation** | Helm install/upgrade | Kubernetes API (bot-manager) |
| **Lifecycle** | Continuous | Per-meeting (2 hours max) |
| **Cleanup** | Manual/upgrade | Automatic TTL |

## Environment Variables

### bot-manager (Kubernetes orchestrator)

| Variable | Description | Default |
|----------|-------------|---------|
| `ORCHESTRATOR` | Orchestrator type | `docker` |
| `K8S_NAMESPACE` | Kubernetes namespace | `default` |
| `K8S_SERVICE_ACCOUNT` | Service account for bot Jobs | `vomeet-bot` |
| `K8S_BOT_IMAGE_REPOSITORY` | Bot image repository | `ghcr.io/voltade/vomeet-bot` |
| `K8S_BOT_IMAGE_TAG` | Bot image tag | `latest` |
| `K8S_BOT_CPU_REQUEST` | CPU request per bot | `1000m` |
| `K8S_BOT_CPU_LIMIT` | CPU limit per bot | `4000m` |
| `K8S_BOT_MEMORY_REQUEST` | Memory request per bot | `2Gi` |
| `K8S_BOT_MEMORY_LIMIT` | Memory limit per bot | `8Gi` |
| `K8S_BOT_ACTIVE_DEADLINE_SECONDS` | Max bot runtime | `7200` (2 hours) |
| `K8S_BOT_TTL_AFTER_FINISHED` | Job cleanup TTL | `300` (5 min) |
| `REDIS_URL` | Redis connection string | Required |
| `WHISPER_LIVE_URL` | WhisperLive WebSocket URL | Required |
| `K8S_BOT_MANAGER_CALLBACK_URL` | Callback URL for bot status | Required |

### Bot Job (BOT_CONFIG env var)

The bot container receives a `BOT_CONFIG` JSON environment variable with:

```json
{
  "meeting_id": 123,
  "platform": "google_meet",
  "meetingUrl": "https://meet.google.com/...",
  "botName": "Vomeet Bot",
  "token": "JWT_TOKEN",
  "nativeMeetingId": "abc-def-ghi",
  "connectionId": "uuid",
  "language": "en",
  "task": "transcribe",
  "redisUrl": "redis://...",
  "container_name": "vomeet-bot-123-abc12345",
  "automaticLeave": {
    "waitingRoomTimeout": 900000,
    "noOneJoinedTimeout": 300000,
    "everyoneLeftTimeout": 120000
  },
  "botManagerCallbackUrl": "http://bot-manager:8080/..."
}
```

## Deployment Workflows

### Staging Deployment

Similar to envoy-crm, create a workflow that:
1. Builds and pushes Docker image with `staging` tag
2. Updates Helm chart with staging values
3. Deploys to staging cluster

Example workflow (`.forgejo/workflows/deploy-staging.yaml`):

```yaml
name: Deploy Staging

on:
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Build and push image
        run: |
          docker build -t ghcr.io/voltade/vomeet-bot:staging ./core
          docker push ghcr.io/voltade/vomeet-bot:staging
      
      - name: Deploy to Kubernetes
        run: |
          helm upgrade --install vomeet-bot ./chart \
            --namespace vomeet-bot-staging \
            --create-namespace \
            -f chart/values.staging.yaml \
            --set imageTag=staging
```

### Production Deployment

Similar workflow with production image tag and cluster.

## Monitoring

### List running bots

```bash
kubectl get jobs -n vomeet-bot -l app.kubernetes.io/name=vomeet-bot
```

### View bot logs

```bash
# Get pod from job
POD=$(kubectl get pods -n vomeet-bot -l app.kubernetes.io/component=bot-instance -o jsonpath='{.items[0].metadata.name}')

# View logs
kubectl logs -n vomeet-bot $POD -f
```

### Check resource usage

```bash
kubectl top pods -n vomeet-bot -l app.kubernetes.io/component=bot-instance
```

## Troubleshooting

### Jobs not starting

1. **Check service account permissions**:
   ```bash
   kubectl describe sa vomeet-bot -n vomeet-bot
   ```

2. **Verify image pull secrets**:
   ```bash
   kubectl get secrets -n vomeet-bot image-pull-secrets
   ```

3. **Check pod events**:
   ```bash
   kubectl describe pod <pod-name> -n vomeet-bot
   ```

### Jobs failing immediately

1. **Check BOT_CONFIG format**:
   ```bash
   kubectl get job <job-name> -n vomeet-bot -o yaml | grep BOT_CONFIG
   ```

2. **Verify secrets are available**:
   ```bash
   kubectl get secrets -n vomeet-bot envs-from-onepassword envs-from-secretstore
   ```

3. **Check container logs**:
   ```bash
   kubectl logs <pod-name> -n vomeet-bot
   ```

### Resource constraints

If bots can't be scheduled:
1. Check node resources: `kubectl top nodes`
2. Adjust resource requests in values.yaml
3. Add nodeSelector/tolerations for dedicated nodes
4. Enable cluster autoscaling

## Scaling Considerations

- **Concurrent bots**: Each bot is a separate Job, so scaling is automatic
- **Node capacity**: Ensure nodes have capacity for expected concurrent bots
- **Resource limits**: Set appropriate CPU/memory limits per bot
- **Job cleanup**: TTL ensures completed jobs are cleaned up automatically

## Security

- ServiceAccounts with minimal permissions
- RBAC for bot-manager (only create/delete Jobs)
- Secrets via External Secrets Operator (not in code)
- Image pull secrets for private registry
- Security contexts for containers (SYS_ADMIN for Chrome sandbox)

## Next Steps

1. **PR Preview Deployments**: Similar to envoy-crm, create isolated namespaces per PR
2. **Monitoring**: Add Prometheus metrics for bot Jobs
3. **Autoscaling**: Consider HPA for bot-manager if needed
4. **Multi-cluster**: Support for staging/prod clusters like envoy-crm
