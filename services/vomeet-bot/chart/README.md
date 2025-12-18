# Vomeet Bot Kubernetes Helm Chart

This Helm chart deploys the vomeet-bot infrastructure to Kubernetes. The chart sets up ServiceAccounts, RBAC, and External Secrets integration. Bot instances are created dynamically as Kubernetes Jobs by the bot-manager service.

## Architecture

- **Ephemeral Bot Instances**: Each meeting bot runs as a Kubernetes Job (not a Deployment)
- **Dynamic Creation**: Bot-manager creates Jobs on-demand via Kubernetes API
- **Resource Management**: Jobs are automatically cleaned up after completion (TTL)
- **Secrets Management**: Uses External Secrets Operator for OnePassword and Kubernetes SecretStore

## Prerequisites

1. Kubernetes cluster (1.24+)
2. Helm 3.x
3. External Secrets Operator installed
4. Image registry access configured
5. Service account with permissions to create Jobs

## Installation

### 1. Install the chart

```bash
# Production
helm install vomeet-bot ./chart \
  --namespace vomeet-bot \
  --create-namespace \
  --set imageTag=production \
  --set imageRepository=ghcr.io/voltade/vomeet-bot

# Staging
helm install vomeet-bot ./chart \
  --namespace vomeet-bot-staging \
  --create-namespace \
  -f values.staging.yaml \
  --set imageTag=staging
```

### 2. Configure External Secrets

Edit the ExternalSecret templates to reference your actual secrets:

- `templates/secret-envs-from-onepassword.yaml` - Add OnePassword vault/item references
- `templates/secret-envs-from-secretstore.yaml` - Add Kubernetes secret references

Required secrets:
- `REDIS_URL` - Redis connection string for command/control
- `WHISPER_LIVE_URL` - WebSocket URL for WhisperLive service

### 3. Configure bot-manager

Set environment variables in bot-manager to use Kubernetes orchestrator:

```yaml
ORCHESTRATOR: k8s
K8S_NAMESPACE: vomeet-bot
K8S_SERVICE_ACCOUNT: vomeet-bot
K8S_BOT_IMAGE_REPOSITORY: ghcr.io/voltade/vomeet-bot
K8S_BOT_IMAGE_TAG: production
K8S_BOT_CPU_REQUEST: 1000m
K8S_BOT_CPU_LIMIT: 4000m
K8S_BOT_MEMORY_REQUEST: 2Gi
K8S_BOT_MEMORY_LIMIT: 8Gi
K8S_BOT_ACTIVE_DEADLINE_SECONDS: 7200
K8S_BOT_TTL_AFTER_FINISHED: 300
REDIS_URL: redis://redis:6379/0
WHISPER_LIVE_URL: ws://whisperlive:8000/ws
K8S_BOT_MANAGER_CALLBACK_URL: http://bot-manager:8080/bots/internal/callback/exited
```

### 4. Grant bot-manager permissions

Bot-manager needs RBAC permissions to create/delete Jobs. Create a ClusterRoleBinding or RoleBinding:

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
    name: bot-manager  # Your bot-manager service account
    namespace: vomeet-bot
```

## Configuration

### Values

Key configuration options in `values.yaml`:

- `imageRepository`: Docker image repository
- `imageTag`: Image tag to use
- `bot.resources`: CPU/memory requests and limits for bot Jobs
- `bot.activeDeadlineSeconds`: Maximum runtime for a bot (default: 7200 = 2 hours)
- `bot.ttlSecondsAfterFinished`: Time to keep completed Jobs before cleanup (default: 300 = 5 min)
- `serviceAccount.create`: Whether to create a service account
- `externalSecrets.enabled`: Enable External Secrets integration

### Resource Sizing

Default bot resources:
- **Production**: 1-4 CPU, 2-8Gi memory
- **Staging**: 0.5-2 CPU, 1-4Gi memory

Adjust based on:
- Concurrent bot count
- Meeting duration
- Browser resource usage (Playwright + Chrome)

## Job Lifecycle

1. **Creation**: Bot-manager creates a Job with unique name (`vomeet-bot-{meeting_id}-{uuid}`)
2. **Execution**: Job pod runs the bot container with `BOT_CONFIG` env var
3. **Completion**: Job completes when meeting ends (success) or fails (error)
4. **Cleanup**: Job is automatically deleted after `ttlSecondsAfterFinished`

## Monitoring

### List running bots

```bash
kubectl get jobs -n vomeet-bot -l app.kubernetes.io/name=vomeet-bot
```

### View bot logs

```bash
# Get pod name from job
kubectl get pods -n vomeet-bot -l app.kubernetes.io/component=bot-instance

# View logs
kubectl logs -n vomeet-bot <pod-name>
```

### Check job status

```bash
kubectl describe job -n vomeet-bot vomeet-bot-<meeting-id>-<uuid>
```

## Troubleshooting

### Jobs not starting

1. Check service account permissions
2. Verify image pull secrets
3. Check resource availability on nodes
4. Review pod events: `kubectl describe pod <pod-name>`

### Jobs failing immediately

1. Check `BOT_CONFIG` format (must be valid JSON)
2. Verify secrets are available
3. Check container logs for errors
4. Ensure Redis/WhisperLive URLs are accessible from pods

### Resource constraints

If bots are being evicted or not scheduled:
1. Increase node resources
2. Adjust resource requests/limits
3. Add nodeSelector/tolerations for dedicated nodes
4. Enable autoscaling

## Comparison with envoy-crm Setup

This chart follows similar patterns to envoy-crm:
- ✅ Helm chart structure
- ✅ External Secrets Operator integration
- ✅ Multi-environment support (staging/prod)
- ✅ ServiceAccount and RBAC
- ⚠️ **Difference**: Uses Jobs instead of Deployments (bots are ephemeral)
- ⚠️ **Difference**: Jobs created dynamically by bot-manager (not via Helm)

## Upgrades

```bash
helm upgrade vomeet-bot ./chart \
  --namespace vomeet-bot \
  --set imageTag=new-tag
```

## Uninstallation

```bash
helm uninstall vomeet-bot --namespace vomeet-bot
```

**Note**: This will NOT delete running bot Jobs. They will continue until completion. To force-delete all bots:

```bash
kubectl delete jobs -n vomeet-bot -l app.kubernetes.io/name=vomeet-bot
```
