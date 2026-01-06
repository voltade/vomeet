"""
Meeting Reconciliation Task

This module provides a background reconciliation system to detect and handle
orphaned meetings - meetings that are stuck in 'active' or 'joining' status
but whose bot containers have terminated unexpectedly.

Industry-standard approach:
1. Periodic reconciliation (every 60 seconds)
2. Detect orphaned meetings by checking Kubernetes job status
3. Properly finalize meetings with appropriate status and metadata
4. Emit webhooks for status changes
5. Comprehensive logging for observability

This handles cases where:
- Bot pod was killed due to activeDeadlineSeconds exceeded
- Bot pod was OOMKilled or crashed
- Bot pod was evicted by Kubernetes
- Network issues prevented callback from reaching bot-manager

Configuration (via environment variables):
- RECONCILIATION_INTERVAL_SECONDS: How often to run reconciliation (default: 60)
- ORPHAN_GRACE_PERIOD_SECONDS: Wait time before considering a meeting orphaned (default: 120)
- RECONCILIATION_MAX_AGE_HOURS: Maximum age of meetings to check (default: 48)

API Endpoints:
- POST /reconcile: Manually trigger reconciliation (useful for debugging)

Usage:
    The reconciliation task is automatically started on bot-manager startup
    and runs in the background. It can also be triggered manually via the API.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from shared_models.database import async_session_local
from shared_models.models import Meeting
from shared_models.schemas import (
    MeetingStatus,
    MeetingCompletionReason,
    MeetingFailureStage,
)

# Configure logging
logger = logging.getLogger("bot_manager.reconciliation")

# Configuration
RECONCILIATION_INTERVAL_SECONDS = int(os.getenv("RECONCILIATION_INTERVAL_SECONDS", "60"))
# Grace period before considering a meeting orphaned (allows for callback delays)
ORPHAN_GRACE_PERIOD_SECONDS = int(os.getenv("ORPHAN_GRACE_PERIOD_SECONDS", "120"))
# Maximum age for meetings to check (don't reconcile very old meetings on every run)
MAX_MEETING_AGE_HOURS = int(os.getenv("RECONCILIATION_MAX_AGE_HOURS", "48"))

# Import Kubernetes client (lazy import to avoid circular imports)
_k8s_batch_api = None
_k8s_namespace = None


def _get_k8s_clients():
    """Lazy initialization of Kubernetes clients."""
    global _k8s_batch_api, _k8s_namespace
    if _k8s_batch_api is None:
        try:
            from kubernetes import client, config
            from kubernetes.client.rest import ApiException

            kubeconfig_path = os.getenv("KUBECONFIG_PATH")
            if kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config()

            _k8s_batch_api = client.BatchV1Api()
            _k8s_namespace = os.getenv("K8S_NAMESPACE", "vomeet")
            logger.info(f"Reconciliation: Initialized Kubernetes client for namespace: {_k8s_namespace}")
        except Exception as e:
            logger.error(f"Reconciliation: Failed to initialize Kubernetes client: {e}")
            return None, None
    return _k8s_batch_api, _k8s_namespace


def _check_kubernetes_job_status(job_name: str) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """
    Check the status of a Kubernetes Job.

    Returns:
        Tuple of (status, reason, metadata):
        - status: 'running', 'succeeded', 'failed', 'not_found', 'unknown'
        - reason: Human-readable reason for the status
        - metadata: Additional metadata about the job status
    """
    from kubernetes.client.rest import ApiException

    batch_api, namespace = _get_k8s_clients()
    if batch_api is None:
        return "unknown", "Kubernetes client not available", None

    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        status = job.status
        metadata = {
            "active": status.active,
            "succeeded": status.succeeded,
            "failed": status.failed,
            "start_time": status.start_time.isoformat() if status.start_time else None,
            "completion_time": status.completion_time.isoformat() if status.completion_time else None,
        }

        # Check conditions for detailed failure reasons
        conditions = status.conditions or []
        for condition in conditions:
            if condition.type == "Failed" and condition.status == "True":
                reason = condition.reason or "Unknown"
                message = condition.message or ""
                metadata["failure_reason"] = reason
                metadata["failure_message"] = message

                # Specific handling for deadline exceeded
                if reason == "DeadlineExceeded":
                    return "failed", f"Job exceeded activeDeadlineSeconds: {message}", metadata
                elif reason == "BackoffLimitExceeded":
                    return "failed", f"Job exceeded backoff limit: {message}", metadata
                else:
                    return "failed", f"Job failed: {reason} - {message}", metadata

        # Check status fields
        if status.active and status.active > 0:
            return "running", "Job is still active", metadata
        elif status.succeeded and status.succeeded > 0:
            return "succeeded", "Job completed successfully", metadata
        elif status.failed and status.failed > 0:
            return "failed", "Job failed (pod failure)", metadata
        else:
            # Job exists but no pods yet (starting up)
            return "starting", "Job is starting", metadata

    except ApiException as e:
        if e.status == 404:
            return "not_found", "Job not found in Kubernetes", None
        logger.warning(f"Kubernetes API error checking job {job_name}: {e.status} {e.reason}")
        return "unknown", f"Kubernetes API error: {e.reason}", None
    except Exception as e:
        logger.error(f"Unexpected error checking job {job_name}: {e}")
        return "unknown", f"Unexpected error: {str(e)}", None


async def _get_orphaned_meetings(db: AsyncSession) -> List[Meeting]:
    """
    Find meetings that may be orphaned (stuck in active states).

    A meeting is potentially orphaned if:
    1. Status is 'active', 'joining', or 'awaiting_admission'
    2. Has a bot_container_id assigned
    3. Was updated more than ORPHAN_GRACE_PERIOD_SECONDS ago
    4. Was created within MAX_MEETING_AGE_HOURS
    """
    cutoff_time = datetime.utcnow() - timedelta(seconds=ORPHAN_GRACE_PERIOD_SECONDS)
    max_age_cutoff = datetime.utcnow() - timedelta(hours=MAX_MEETING_AGE_HOURS)

    # Statuses that should have an active bot
    active_statuses = [
        MeetingStatus.ACTIVE.value,
        MeetingStatus.JOINING.value,
        MeetingStatus.AWAITING_ADMISSION.value,
    ]

    stmt = (
        select(Meeting)
        .where(
            and_(
                Meeting.status.in_(active_statuses),
                Meeting.bot_container_id.isnot(None),
                Meeting.updated_at < cutoff_time,
                Meeting.created_at > max_age_cutoff,
            )
        )
        .order_by(Meeting.updated_at.asc())
    )

    result = await db.execute(stmt)
    return result.scalars().all()


async def _finalize_orphaned_meeting(
    meeting: Meeting,
    db: AsyncSession,
    job_status: str,
    reason: str,
    metadata: Optional[Dict[str, Any]],
) -> bool:
    """
    Finalize an orphaned meeting with the appropriate status.

    Args:
        meeting: The orphaned meeting to finalize
        db: Database session
        job_status: Status of the Kubernetes job
        reason: Reason for the status
        metadata: Additional metadata from Kubernetes

    Returns:
        True if meeting was finalized, False otherwise
    """
    # Import here to avoid circular imports
    from app.main import update_meeting_status, publish_meeting_status_change, redis_client

    old_status = meeting.status

    # Determine the appropriate final status
    if job_status in ["succeeded", "not_found"]:
        # Job completed or was cleaned up - mark as completed
        # This handles the case where the job finished but callback was lost
        new_status = MeetingStatus.COMPLETED
        completion_reason = MeetingCompletionReason.NORMAL
        failure_stage = None

        # If job not found, it might have been cleaned up after deadline exceeded
        if job_status == "not_found":
            completion_reason = MeetingCompletionReason.STOPPED

    elif job_status == "failed":
        # Job explicitly failed
        new_status = MeetingStatus.FAILED
        completion_reason = None

        # Determine failure stage based on current meeting status
        if meeting.status == MeetingStatus.JOINING.value:
            failure_stage = MeetingFailureStage.JOINING
        elif meeting.status == MeetingStatus.AWAITING_ADMISSION.value:
            failure_stage = MeetingFailureStage.WAITING_ROOM
        else:
            failure_stage = MeetingFailureStage.RECORDING
    else:
        # Unknown status - don't finalize yet
        logger.warning(f"Reconciliation: Meeting {meeting.id} has unknown job status '{job_status}', skipping")
        return False

    # Build transition metadata
    transition_metadata = {
        "reconciliation_job_status": job_status,
        "reconciliation_reason": reason,
        "reconciled_at": datetime.utcnow().isoformat(),
        "reconciled_from_status": old_status,
    }
    if metadata:
        transition_metadata["kubernetes_metadata"] = metadata

    # Update the meeting status
    logger.info(
        f"Reconciliation: Finalizing meeting {meeting.id} from '{old_status}' to '{new_status.value}' "
        f"(job_status={job_status}, reason={reason})"
    )

    success = await update_meeting_status(
        meeting,
        new_status,
        db,
        completion_reason=completion_reason,
        failure_stage=failure_stage,
        error_details=reason if job_status == "failed" else None,
        transition_reason="reconciliation",
        transition_metadata=transition_metadata,
    )

    if success:
        # Publish status change via Redis
        try:
            await publish_meeting_status_change(
                meeting_id=meeting.id,
                new_status=new_status.value,
                redis_client=redis_client,
                platform=meeting.platform,
                native_meeting_id=meeting.platform_specific_id,
                user_id=meeting.user_id or 0,
            )
        except Exception as e:
            logger.error(f"Reconciliation: Failed to publish status change for meeting {meeting.id}: {e}")

        # Trigger bot exit tasks (webhooks, transcript aggregation, etc.)
        try:
            from app.tasks.bot_exit_tasks import run_all_tasks

            await run_all_tasks(meeting.id)
        except Exception as e:
            logger.error(f"Reconciliation: Failed to run exit tasks for meeting {meeting.id}: {e}")

        logger.info(f"Reconciliation: Successfully finalized meeting {meeting.id} ({old_status} -> {new_status.value})")
    else:
        logger.error(f"Reconciliation: Failed to update status for meeting {meeting.id}")

    return success


async def reconcile_orphaned_meetings() -> Dict[str, Any]:
    """
    Main reconciliation function that finds and handles orphaned meetings.

    Returns:
        Summary of reconciliation results
    """
    results = {
        "checked": 0,
        "finalized": 0,
        "still_running": 0,
        "errors": 0,
        "details": [],
    }

    logger.info("Reconciliation: Starting orphaned meeting reconciliation...")

    try:
        async with async_session_local() as db:
            # Find potentially orphaned meetings
            orphaned_meetings = await _get_orphaned_meetings(db)
            results["checked"] = len(orphaned_meetings)

            if not orphaned_meetings:
                logger.debug("Reconciliation: No potentially orphaned meetings found")
                return results

            logger.info(f"Reconciliation: Found {len(orphaned_meetings)} potentially orphaned meetings")

            for meeting in orphaned_meetings:
                try:
                    # Check Kubernetes job status
                    job_name = meeting.bot_container_id
                    job_status, reason, metadata = _check_kubernetes_job_status(job_name)

                    detail = {
                        "meeting_id": meeting.id,
                        "container_id": job_name,
                        "current_status": meeting.status,
                        "job_status": job_status,
                        "reason": reason,
                    }

                    if job_status == "running":
                        # Job is still running, not orphaned
                        logger.debug(f"Reconciliation: Meeting {meeting.id} job {job_name} is still running")
                        results["still_running"] += 1
                        detail["action"] = "skipped_still_running"

                    elif job_status in ["failed", "succeeded", "not_found"]:
                        # Job is done or gone - finalize the meeting
                        success = await _finalize_orphaned_meeting(meeting, db, job_status, reason, metadata)
                        if success:
                            results["finalized"] += 1
                            detail["action"] = "finalized"
                            detail["new_status"] = meeting.status
                        else:
                            results["errors"] += 1
                            detail["action"] = "finalization_failed"
                    else:
                        # Unknown or starting - skip for now
                        logger.debug(f"Reconciliation: Meeting {meeting.id} job status is '{job_status}', skipping")
                        detail["action"] = "skipped_unknown_status"

                    results["details"].append(detail)

                except Exception as e:
                    logger.error(f"Reconciliation: Error processing meeting {meeting.id}: {e}", exc_info=True)
                    results["errors"] += 1
                    results["details"].append(
                        {
                            "meeting_id": meeting.id,
                            "action": "error",
                            "error": str(e),
                        }
                    )

            await db.commit()

    except Exception as e:
        logger.error(f"Reconciliation: Fatal error during reconciliation: {e}", exc_info=True)
        results["errors"] += 1

    logger.info(
        f"Reconciliation: Completed - checked={results['checked']}, "
        f"finalized={results['finalized']}, still_running={results['still_running']}, "
        f"errors={results['errors']}"
    )

    return results


class MeetingReconciliationTask:
    """
    Background task that periodically reconciles orphaned meetings.

    This task runs continuously in the background and checks for orphaned
    meetings at a configurable interval.
    """

    def __init__(self, interval_seconds: int = RECONCILIATION_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _run_loop(self):
        """Main reconciliation loop."""
        logger.info(f"Reconciliation: Starting background task with interval={self.interval_seconds}s")

        # Initial delay to allow services to stabilize on startup
        await asyncio.sleep(30)

        while self._running:
            try:
                await reconcile_orphaned_meetings()
            except Exception as e:
                logger.error(f"Reconciliation: Error in reconciliation loop: {e}", exc_info=True)

            # Wait for next interval
            await asyncio.sleep(self.interval_seconds)

        logger.info("Reconciliation: Background task stopped")

    def start(self):
        """Start the background reconciliation task."""
        if self._running:
            logger.warning("Reconciliation: Task already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Reconciliation: Background task started")

    def stop(self):
        """Stop the background reconciliation task."""
        logger.info("Reconciliation: Stopping background task...")
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None


# Global instance for the reconciliation task
_reconciliation_task: Optional[MeetingReconciliationTask] = None


def get_reconciliation_task() -> MeetingReconciliationTask:
    """Get or create the global reconciliation task instance."""
    global _reconciliation_task
    if _reconciliation_task is None:
        _reconciliation_task = MeetingReconciliationTask()
    return _reconciliation_task


async def start_reconciliation_task():
    """Start the global reconciliation task."""
    task = get_reconciliation_task()
    task.start()


async def stop_reconciliation_task():
    """Stop the global reconciliation task."""
    global _reconciliation_task
    if _reconciliation_task:
        _reconciliation_task.stop()
        _reconciliation_task = None
