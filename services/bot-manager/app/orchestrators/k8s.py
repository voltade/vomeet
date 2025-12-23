"""Kubernetes orchestrator implementation.

This module provides Kubernetes Job-based orchestration for vomeet-bot instances.
Each bot instance is created as a Kubernetes Job that runs until the meeting ends.
"""

from __future__ import annotations

import os
import uuid
import json
import logging
from typing import Optional, Tuple, Dict, Any, List
from datetime import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException
import httpx

from app.orchestrators.common import (
    enforce_user_concurrency_limit,
    count_user_active_bots,
)

logger = logging.getLogger("bot_manager.k8s_utils")

# Kubernetes configuration
KUBECONFIG_PATH = os.getenv("KUBECONFIG")
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
K8S_IMAGE_REPOSITORY = os.getenv("K8S_BOT_IMAGE_REPOSITORY", "ghcr.io/voltade/vomeet-bot")
K8S_IMAGE_TAG = os.getenv("K8S_BOT_IMAGE_TAG", "latest")
K8S_SERVICE_ACCOUNT = os.getenv("K8S_SERVICE_ACCOUNT", "vomeet-bot")
K8S_REDIS_URL = os.getenv("REDIS_URL")  # Required for bot config
K8S_WHISPER_LIVE_URL = os.getenv("WHISPER_LIVE_URL", "wss://vomeet-whisper-proxy.voltade.workers.dev/ws")
K8S_BOT_MANAGER_CALLBACK_URL = os.getenv(
    "K8S_BOT_MANAGER_CALLBACK_URL",
    "http://vomeet-bot-manager:8080/bots/internal/callback/exited",
)

# Resource limits (can be overridden via env)
K8S_BOT_CPU_REQUEST = os.getenv("K8S_BOT_CPU_REQUEST", "1000m")
K8S_BOT_CPU_LIMIT = os.getenv("K8S_BOT_CPU_LIMIT", "4000m")
K8S_BOT_MEMORY_REQUEST = os.getenv("K8S_BOT_MEMORY_REQUEST", "2Gi")
K8S_BOT_MEMORY_LIMIT = os.getenv("K8S_BOT_MEMORY_LIMIT", "8Gi")
K8S_BOT_ACTIVE_DEADLINE = int(os.getenv("K8S_BOT_ACTIVE_DEADLINE_SECONDS", "7200"))  # 2 hours
K8S_BOT_TTL_AFTER_FINISHED = int(os.getenv("K8S_BOT_TTL_AFTER_FINISHED", "300"))  # 5 minutes

# Initialize Kubernetes client
try:
    if KUBECONFIG_PATH:
        config.load_kube_config(config_file=KUBECONFIG_PATH)
    else:
        # Try in-cluster config first, fallback to kubeconfig
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

    batch_api = client.BatchV1Api()
    core_api = client.CoreV1Api()
    logger.info(f"Initialized Kubernetes client for namespace: {K8S_NAMESPACE}")
except Exception as e:
    logger.error(f"Failed to initialize Kubernetes client: {e}")
    batch_api = None
    core_api = None


# ---------------------------------------------------------------------------
# Helper / compatibility no-ops ------------------------------------------------


def get_socket_session(*_args, **_kwargs):  # type: ignore
    """Return None – kept for API compatibility (Docker-specific concept)."""
    return None


def close_client():  # type: ignore
    """No persistent Kubernetes client to close."""
    return None


close_docker_client = close_client  # compatibility alias


# ---------------------------------------------------------------------------
# Core public API -------------------------------------------------------------


async def start_bot_container(
    user_id: int,
    meeting_id: int,
    meeting_url: Optional[str],
    platform: str,
    bot_name: Optional[str],
    user_token: str,
    native_meeting_id: str,
    language: Optional[str],
    task: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Create a Kubernetes Job for a vomeet-bot instance.

    Returns (job_name, connection_id) on success.
    """
    if batch_api is None:
        logger.error("Kubernetes client not initialized")
        return None, None

    connection_id = str(uuid.uuid4())

    # Mint MeetingToken (HS256)
    from app.main import mint_meeting_token

    try:
        meeting_token = mint_meeting_token(
            meeting_id=meeting_id,
            user_id=user_id,
            platform=platform,
            native_meeting_id=native_meeting_id,
            ttl_seconds=7200,  # 2 hours
        )
    except Exception as token_err:
        logger.error(
            f"Failed to mint MeetingToken for meeting {meeting_id}: {token_err}",
            exc_info=True,
        )
        return None, None

    # Construct BOT_CONFIG JSON
    bot_config_data = {
        "meeting_id": meeting_id,
        "platform": platform,
        "meetingUrl": meeting_url,
        "botName": bot_name or "Voltade Meeting Assistant",
        "token": meeting_token,
        "nativeMeetingId": native_meeting_id,
        "connectionId": connection_id,
        "language": language,
        "task": task,
        "redisUrl": K8S_REDIS_URL,
        "container_name": f"vomeet-bot-{meeting_id}-{connection_id[:8]}",
        "automaticLeave": {
            "waitingRoomTimeout": 900000,
            "noOneJoinedTimeout": 120000,
            "everyoneLeftTimeout": 60000,
        },
        "botManagerCallbackUrl": K8S_BOT_MANAGER_CALLBACK_URL,
    }

    # Remove None values
    cleaned_config_data = {k: v for k, v in bot_config_data.items() if v is not None}
    bot_config_json = json.dumps(cleaned_config_data)

    # Generate unique job name (Kubernetes names must be DNS-1123 subdomain)
    job_name = f"vomeet-bot-{meeting_id}-{connection_id[:8]}"
    job_name = job_name.lower().replace("_", "-")[:63]  # Ensure valid DNS name

    # Create Job manifest (use snake_case for k8s python client compatibility)
    job_manifest = {
        "api_version": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": K8S_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "vomeet-bot",
                "app.kubernetes.io/component": "bot-instance",
                "app.kubernetes.io/managed-by": "bot-manager",
                "meeting-id": str(meeting_id),
                "user-id": str(user_id),
                "platform": platform,
                "connection-id": connection_id,
                "native-meeting-id": native_meeting_id,
            },
            "annotations": {
                "bot-manager/created-at": datetime.utcnow().isoformat(),
            },
        },
        "spec": {
            "activeDeadlineSeconds": K8S_BOT_ACTIVE_DEADLINE,
            "ttlSecondsAfterFinished": K8S_BOT_TTL_AFTER_FINISHED,
            "backoffLimit": 0,  # Don't retry failed bots
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "vomeet-bot",
                        "app.kubernetes.io/component": "bot-instance",
                        "meeting-id": str(meeting_id),
                        "user-id": str(user_id),
                        "platform": platform,
                        "connection-id": connection_id,
                        "native-meeting-id": native_meeting_id,
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": K8S_SERVICE_ACCOUNT,
                    "containers": [
                        {
                            "name": "vomeet-bot",
                            "image": f"{K8S_IMAGE_REPOSITORY}:{K8S_IMAGE_TAG}",
                            "imagePullPolicy": "Always",
                            "resources": {
                                "requests": {
                                    "cpu": K8S_BOT_CPU_REQUEST,
                                    "memory": K8S_BOT_MEMORY_REQUEST,
                                },
                                "limits": {
                                    "cpu": K8S_BOT_CPU_LIMIT,
                                    "memory": K8S_BOT_MEMORY_LIMIT,
                                },
                            },
                            "env": [
                                {"name": "BOT_CONFIG", "value": bot_config_json},
                                {
                                    "name": "WHISPER_LIVE_URL",
                                    "value": K8S_WHISPER_LIVE_URL,
                                },
                            ],
                            "securityContext": {
                                "capabilities": {
                                    "add": ["SYS_ADMIN"]  # Required for Chrome sandbox
                                }
                            },
                            "volumeMounts": [
                                {"name": "tmp", "mountPath": "/tmp"},
                                {"name": "storage", "mountPath": "/app/storage"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                        {"name": "storage", "emptyDir": {}},
                    ],
                },
            },
        },
    }

    logger.info(f"Creating Kubernetes Job '{job_name}' for meeting {meeting_id} in namespace {K8S_NAMESPACE}")

    try:
        # Create the Job - pass manifest dict directly (not V1Job object)
        job = batch_api.create_namespaced_job(namespace=K8S_NAMESPACE, body=job_manifest)

        logger.info(
            f"Successfully created Kubernetes Job '{job_name}' (UID: {job.metadata.uid}). "
            f"Connection ID: {connection_id}"
        )

        return job_name, connection_id

    except ApiException as e:
        logger.error(
            f"Kubernetes API error creating Job '{job_name}': HTTP {e.status}, Reason: {e.reason}, Body: {e.body}"
        )
        return None, None
    except Exception as e:
        logger.exception(f"Unexpected error creating Kubernetes Job '{job_name}': {e}")
        return None, None


def stop_bot_container(container_id: str) -> bool:
    """Stop a Kubernetes Job by name (container_id is the job name).

    Uses the Kubernetes API to delete the job, which will terminate the pod.
    """
    if batch_api is None:
        logger.error("Kubernetes client not initialized")
        return False

    job_name = container_id
    logger.info(f"Stopping Kubernetes Job '{job_name}' in namespace {K8S_NAMESPACE}")

    try:
        # Delete the job with propagation policy to also delete pods
        delete_options = client.V1DeleteOptions(propagation_policy="Background")
        batch_api.delete_namespaced_job(name=job_name, namespace=K8S_NAMESPACE, body=delete_options)
        logger.info(f"Successfully deleted Kubernetes Job '{job_name}'")
        return True

    except ApiException as e:
        if e.status == 404:
            logger.warning(f"Job '{job_name}' not found (may already be deleted)")
            return True  # Consider it successful if already gone
        logger.error(
            f"Kubernetes API error deleting Job '{job_name}': HTTP {e.status}, Reason: {e.reason}, Body: {e.body}"
        )
        return False
    except Exception as e:
        logger.exception(f"Unexpected error deleting Kubernetes Job '{job_name}': {e}")
        return False


async def get_running_bots_status(user_id: int) -> List[Dict[str, Any]]:
    """Return a list of running bots for the given user by querying Kubernetes API.

    Queries Kubernetes Jobs with label selector to find all running vomeet-bot jobs
    for the specified user.
    """
    if batch_api is None:
        logger.error("Kubernetes client not initialized")
        return []

    logger.info(f"Querying Kubernetes for running bots for user {user_id}")

    try:
        # List jobs with label selector
        label_selector = f"app.kubernetes.io/name=vomeet-bot,user-id={user_id}"

        jobs = batch_api.list_namespaced_job(namespace=K8S_NAMESPACE, label_selector=label_selector)

        running_bots = []

        for job in jobs.items:
            job_status = job.status

            # Determine normalized status
            normalized = None
            if job_status.active:
                normalized = "Up"
            elif job_status.succeeded:
                normalized = "Exited"
            elif job_status.failed:
                normalized = "Failed"
            else:
                normalized = "Starting"

            # Get labels and annotations
            labels = job.metadata.labels or {}
            annotations = job.metadata.annotations or {}

            # Get pod status if available
            container_id = job.metadata.name
            container_name = labels.get("container_name", container_id)

            bot_status = {
                "container_id": container_id,
                "container_name": container_name,
                "platform": labels.get("platform"),
                "native_meeting_id": labels.get("native-meeting-id") or labels.get("native_meeting_id"),
                "status": normalized,
                "normalized_status": normalized,
                "created_at": annotations.get(
                    "bot-manager/created-at",
                    job.metadata.creation_timestamp.isoformat() if job.metadata.creation_timestamp else None,
                ),
                "labels": labels,
                "meeting_id_from_name": labels.get("meeting-id"),
            }

            running_bots.append(bot_status)
            logger.debug(f"Found running bot: {bot_status}")

        logger.info(f"Found {len(running_bots)} running bots for user {user_id}")
        return running_bots

    except ApiException as e:
        logger.error(f"Kubernetes API error querying Jobs: HTTP {e.status}, Reason: {e.reason}, Body: {e.body}")
        return []
    except Exception as e:
        logger.exception(f"Unexpected error querying Kubernetes for running bots: {e}")
        return []


async def verify_container_running(container_id: str) -> bool:
    """Return True if the Kubernetes Job is still running.

    Queries the Kubernetes API to check if the job/pod is still active.
    """
    if batch_api is None:
        logger.error("Kubernetes client not initialized")
        return False

    job_name = container_id
    logger.debug(f"Verifying if Kubernetes Job '{job_name}' is still running")

    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=K8S_NAMESPACE)

        # Job is running if it has active pods
        is_running = job.status.active is not None and job.status.active > 0

        logger.debug(
            f"Job '{job_name}' status - Active: {job.status.active}, "
            f"Succeeded: {job.status.succeeded}, Failed: {job.status.failed}, "
            f"Running: {is_running}"
        )
        return is_running

    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Job '{job_name}' not found (404), not running")
            return False
        logger.warning(f"Kubernetes API error checking Job '{job_name}': HTTP {e.status}, Reason: {e.reason}")
        return False
    except Exception as e:
        logger.warning(f"Unexpected error checking Job '{job_name}': {e}")
        return False


# Alias for shared function – import lazily to avoid circulars
from app.orchestrator_utils import _record_session_start  # noqa: E402
