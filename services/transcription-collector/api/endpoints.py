import logging
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query, Header
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from shared_models.database import get_db
from shared_models.models import Account, Meeting, Transcription, MeetingSession, AudioChunk
from shared_models.schemas import (
    HealthResponse,
    MeetingResponse,
    MeetingListResponse,
    TranscriptionResponse,
    Platform,
    TranscriptionSegment,
    MeetingUpdate,
    MeetingCreate,
    MeetingStatus,
)

from api.auth import get_account_from_api_key
from streaming.processors import verify_meeting_token

logger = logging.getLogger(__name__)
router = APIRouter()


class WsMeetingRef(MeetingCreate):
    """
    Schema for WS subscription meeting reference.
    Inherits validation from MeetingCreate but only platform and native_meeting_id are relevant.
    """

    class Config:
        extra = "ignore"


class WsAuthorizeSubscribeRequest(BaseModel):
    meetings: List[WsMeetingRef]


class WsAuthorizeSubscribeResponse(BaseModel):
    authorized: List[Dict[str, str]]
    errors: List[str] = []
    account_id: Optional[int] = None  # Include account_id for channel isolation


async def _get_full_transcript_segments(
    internal_meeting_id: int, db: AsyncSession, redis_c: aioredis.Redis
) -> List[TranscriptionSegment]:
    """
    Core logic to fetch and merge transcript segments from PG and Redis.
    """
    logger.debug(f"[_get_full_transcript_segments] Fetching for meeting ID {internal_meeting_id}")

    # 1. Fetch session start times for this meeting
    stmt_sessions = select(MeetingSession).where(MeetingSession.meeting_id == internal_meeting_id)
    result_sessions = await db.execute(stmt_sessions)
    sessions = result_sessions.scalars().all()
    session_times: Dict[str, datetime] = {session.session_uid: session.session_start_time for session in sessions}
    if not session_times:
        logger.warning(
            f"[_get_full_transcript_segments] No session start times found in DB for meeting {internal_meeting_id}."
        )

    # 2. Fetch transcript segments from PostgreSQL (immutable segments - legacy)
    stmt_transcripts = select(Transcription).where(Transcription.meeting_id == internal_meeting_id)
    result_transcripts = await db.execute(stmt_transcripts)
    db_segments = result_transcripts.scalars().all()

    # 2b. Fetch audio chunks from new AudioChunk table (CF Proxy transcriptions)
    stmt_chunks = (
        select(AudioChunk).where(AudioChunk.meeting_id == internal_meeting_id).order_by(AudioChunk.chunk_index)
    )
    result_chunks = await db.execute(stmt_chunks)
    audio_chunks = result_chunks.scalars().all()

    # 3. Fetch segments from Redis (mutable segments)
    hash_key = f"meeting:{internal_meeting_id}:segments"
    redis_segments_raw = {}
    if redis_c:
        try:
            redis_segments_raw = await redis_c.hgetall(hash_key)
        except Exception as e:
            logger.error(
                f"[_get_full_transcript_segments] Failed to fetch from Redis hash {hash_key}: {e}",
                exc_info=True,
            )

    # 4. Calculate absolute times and merge segments
    merged_segments_with_abs_time: Dict[str, Tuple[datetime, TranscriptionSegment]] = {}

    for segment in db_segments:
        key = f"{segment.start_time:.3f}"
        session_uid = segment.session_uid
        session_start = session_times.get(session_uid) if session_uid else None

        # Try to calculate absolute time from session start, fallback to created_at
        absolute_start_time = None
        absolute_end_time = None

        if session_start:
            try:
                if session_start.tzinfo is None:
                    session_start = session_start.replace(tzinfo=timezone.utc)
                absolute_start_time = session_start + timedelta(seconds=segment.start_time)
                absolute_end_time = session_start + timedelta(seconds=segment.end_time)
            except Exception as calc_err:
                logger.warning(
                    f"[API Meet {internal_meeting_id}] Error calculating absolute time from session for segment {key}: {calc_err}"
                )

        # Fallback: use created_at as absolute time if session_start not available
        if absolute_start_time is None and segment.created_at:
            created_at = segment.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            # Use created_at minus the relative times as an approximation
            # This assumes created_at is close to when the segment ended
            absolute_start_time = created_at - timedelta(seconds=(segment.end_time - segment.start_time))
            absolute_end_time = created_at

        # Create segment object regardless of session match
        segment_obj = TranscriptionSegment(
            start_time=segment.start_time,
            end_time=segment.end_time,
            text=segment.text,
            language=segment.language,
            speaker=segment.speaker,
            created_at=segment.created_at,
            absolute_start_time=absolute_start_time,
            absolute_end_time=absolute_end_time,
        )

        # Use absolute_start_time for sorting if available, otherwise created_at or a fixed time
        sort_time = absolute_start_time or (
            segment.created_at.replace(tzinfo=timezone.utc)
            if segment.created_at
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        merged_segments_with_abs_time[key] = (sort_time, segment_obj)

    for start_time_str, segment_json in redis_segments_raw.items():
        try:
            segment_data = json.loads(segment_json)
            session_uid_from_redis = segment_data.get("session_uid")
            potential_session_key = session_uid_from_redis
            if session_uid_from_redis:
                # This logic to strip prefixes is brittle. A better solution would be to store the canonical session_uid.
                # For now, keeping it to match previous behavior.
                prefixes_to_check = [f"{p.value}_" for p in Platform]
                for prefix in prefixes_to_check:
                    if session_uid_from_redis.startswith(prefix):
                        potential_session_key = session_uid_from_redis[len(prefix) :]
                        break
            session_start = session_times.get(potential_session_key) if potential_session_key else None

            # Must have at least end_time and text
            if "end_time" not in segment_data or "text" not in segment_data:
                continue

            relative_start_time = float(start_time_str)
            absolute_start_time = None
            absolute_end_time = None

            if session_start:
                if session_start.tzinfo is None:
                    session_start = session_start.replace(tzinfo=timezone.utc)
                absolute_start_time = session_start + timedelta(seconds=relative_start_time)
                absolute_end_time = session_start + timedelta(seconds=segment_data["end_time"])
            else:
                # Fallback: use current time as approximation when no session found
                now = datetime.now(timezone.utc)
                # Approximate absolute times based on relative segment times
                duration = segment_data["end_time"] - relative_start_time
                absolute_end_time = now
                absolute_start_time = now - timedelta(seconds=duration)

            segment_obj = TranscriptionSegment(
                start_time=relative_start_time,
                end_time=segment_data["end_time"],
                text=segment_data["text"],
                language=segment_data.get("language"),
                speaker=segment_data.get("speaker"),
                absolute_start_time=absolute_start_time,
                absolute_end_time=absolute_end_time,
            )

            # Use absolute_start_time for sorting
            sort_time = absolute_start_time or datetime.now(timezone.utc)
            merged_segments_with_abs_time[start_time_str] = (
                sort_time,
                segment_obj,
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.error(
                f"[_get_full_transcript_segments] Error parsing Redis segment {start_time_str} for meeting {internal_meeting_id}: {e}"
            )

    # 4b. Process AudioChunk records (new CF Proxy format)
    # Audio chunks have chunk_timestamp and chunk_index for ordering
    # Duration is typically 10 seconds per chunk
    CHUNK_DURATION_MS = 10000  # 10 seconds default
    for chunk in audio_chunks:
        chunk_base_time = datetime.fromtimestamp(chunk.chunk_timestamp / 1000, tz=timezone.utc)

        # If chunk has detailed segments, expand them
        if chunk.segments:
            for seg in chunk.segments:
                seg_start = seg.get("start", 0)
                seg_end = seg.get("end", chunk.duration or 10.0)
                seg_text = seg.get("text", "")

                if not seg_text or not seg_text.strip():
                    continue

                # Calculate absolute time: chunk base + segment relative time
                absolute_start_time = chunk_base_time + timedelta(seconds=seg_start)
                absolute_end_time = chunk_base_time + timedelta(seconds=seg_end)

                segment_obj = TranscriptionSegment(
                    start_time=seg_start,
                    end_time=seg_end,
                    text=seg_text.strip(),
                    language=chunk.language,
                    speaker=chunk.speaker,
                    absolute_start_time=absolute_start_time,
                    absolute_end_time=absolute_end_time,
                    created_at=chunk.created_at,
                )

                # Use unique key to avoid duplicates with same start time from different chunks
                key = f"chunk_{chunk.id}_{seg_start:.3f}"
                merged_segments_with_abs_time[key] = (absolute_start_time, segment_obj)
        elif chunk.full_text and chunk.full_text.strip():
            # No detailed segments, use full_text as single segment
            duration = chunk.duration or 10.0
            absolute_start_time = chunk_base_time
            absolute_end_time = chunk_base_time + timedelta(seconds=duration)

            segment_obj = TranscriptionSegment(
                start_time=0.0,
                end_time=duration,
                text=chunk.full_text.strip(),
                language=chunk.language,
                speaker=chunk.speaker,
                absolute_start_time=absolute_start_time,
                absolute_end_time=absolute_end_time,
                created_at=chunk.created_at,
            )

            key = f"chunk_{chunk.id}_full"
            merged_segments_with_abs_time[key] = (absolute_start_time, segment_obj)

    # 5. Sort based on calculated absolute time and return
    sorted_segment_tuples = sorted(merged_segments_with_abs_time.values(), key=lambda item: item[0])
    segments = [segment_obj for abs_time, segment_obj in sorted_segment_tuples]

    # 6. Deduplicate overlapping or near-duplicate segments
    deduped: List[TranscriptionSegment] = []
    for seg in segments:
        if not deduped:
            deduped.append(seg)
            continue

        last = deduped[-1]
        seg_text = (seg.text or "").strip().lower()
        last_text = (last.text or "").strip().lower()

        # Check for similar text (exact match or one contains the other)
        same_text = seg_text == last_text
        text_overlap = (seg_text in last_text) or (last_text in seg_text) if seg_text and last_text else False

        # Use absolute times for overlap detection (more accurate across chunks)
        abs_overlaps = False
        if seg.absolute_start_time and seg.absolute_end_time and last.absolute_start_time and last.absolute_end_time:
            abs_overlaps = (
                seg.absolute_start_time < last.absolute_end_time and seg.absolute_end_time > last.absolute_start_time
            )
            # Also check for very close timestamps (within 2 seconds)
            time_diff = abs((seg.absolute_start_time - last.absolute_start_time).total_seconds())
            close_in_time = time_diff < 2.0
        else:
            # Fallback to relative times
            abs_overlaps = max(seg.start_time, last.start_time) < min(seg.end_time, last.end_time)
            close_in_time = abs(seg.start_time - last.start_time) < 2.0

        if (same_text or text_overlap) and (abs_overlaps or close_in_time):
            # Keep the longer/more complete segment
            if len(seg_text) > len(last_text):
                deduped[-1] = seg
            continue

        deduped.append(seg)

    # 7. Merge consecutive segments from the same speaker
    # This creates more readable paragraphs instead of one-line segments
    merged: List[TranscriptionSegment] = []
    MAX_MERGED_DURATION = 60.0  # Maximum duration for a merged segment in seconds
    MAX_GAP_SECONDS = 5.0  # Maximum gap between segments to merge

    for seg in deduped:
        if not merged:
            merged.append(seg)
            continue

        last = merged[-1]
        same_speaker = (seg.speaker or "Unknown") == (last.speaker or "Unknown")

        # Calculate time gap between segments
        gap_seconds = 0.0
        if seg.absolute_start_time and last.absolute_end_time:
            gap_seconds = (seg.absolute_start_time - last.absolute_end_time).total_seconds()
        else:
            gap_seconds = seg.start_time - last.end_time

        # Calculate current merged segment duration
        current_duration = 0.0
        if last.absolute_end_time and last.absolute_start_time:
            current_duration = (last.absolute_end_time - last.absolute_start_time).total_seconds()
        else:
            current_duration = last.end_time - last.start_time

        # Merge if same speaker, gap is small, and merged segment won't be too long
        should_merge = (
            same_speaker
            and gap_seconds >= 0
            and gap_seconds < MAX_GAP_SECONDS
            and current_duration < MAX_MERGED_DURATION
        )

        if should_merge:
            # Combine text with space
            combined_text = f"{last.text} {seg.text}".strip()

            # Update the last segment with merged data
            merged[-1] = TranscriptionSegment(
                start_time=last.start_time,
                end_time=seg.end_time,
                text=combined_text,
                language=last.language or seg.language,
                speaker=last.speaker or seg.speaker,
                absolute_start_time=last.absolute_start_time,
                absolute_end_time=seg.absolute_end_time,
                created_at=last.created_at,
            )
        else:
            merged.append(seg)

    return merged


@router.get("/healthz")
async def healthz():
    """Simple health check for k8s probes"""
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    """Detailed health check endpoint"""
    redis_status = "healthy"
    db_status = "healthy"

    try:
        redis_c = getattr(request.app.state, "redis_client", None)
        if not redis_c:
            raise ValueError("Redis client not initialized in app.state")
        await redis_c.ping()
    except Exception as e:
        redis_status = f"unhealthy: {str(e)}"

    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    return HealthResponse(
        status="healthy" if redis_status == "healthy" and db_status == "healthy" else "unhealthy",
        redis=redis_status,
        database=db_status,
        timestamp=datetime.now().isoformat(),
    )


@router.get(
    "/meetings",
    response_model=MeetingListResponse,
    summary="Get list of all meetings for the current account",
    dependencies=[Depends(get_account_from_api_key)],
)
async def get_meetings(account: Account = Depends(get_account_from_api_key), db: AsyncSession = Depends(get_db)):
    """Returns a list of all meetings initiated by the authenticated account."""
    stmt = select(Meeting).where(Meeting.account_id == account.id).order_by(Meeting.created_at.desc())
    result = await db.execute(stmt)
    meetings = result.scalars().all()
    return MeetingListResponse(meetings=[MeetingResponse.model_validate(m) for m in meetings])


@router.get(
    "/transcripts/{platform}/{native_meeting_id}",
    response_model=TranscriptionResponse,
    summary="Get transcript for a specific meeting by platform and native ID",
    dependencies=[Depends(get_account_from_api_key)],
)
async def get_transcript_by_native_id(
    platform: Platform,
    native_meeting_id: str,
    request: Request,  # Added for redis_client access
    meeting_id: Optional[int] = Query(
        None,
        description="Optional specific database meeting ID. If provided, returns that exact meeting. If not provided, returns the latest meeting for the platform/native_meeting_id combination.",
    ),
    account: Account = Depends(get_account_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves the meeting details and transcript segments for a meeting specified by its platform and native ID.

    Behavior:
    - If meeting_id is provided: Returns the exact meeting with that database ID (must belong to account and match platform/native_meeting_id)
    - If meeting_id is not provided: Returns the latest matching meeting record for the account (backward compatible behavior)

    Combines data from both PostgreSQL (immutable segments) and Redis Hashes (mutable segments).
    """
    logger.debug(
        f"[API] Account {account.id} requested transcript for {platform.value} / {native_meeting_id}, meeting_id={meeting_id}"
    )
    redis_c = getattr(request.app.state, "redis_client", None)

    if meeting_id is not None:
        # Get specific meeting by database ID
        stmt_meeting = select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.account_id == account.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id,
        )
        logger.debug(f"[API] Looking for specific meeting ID {meeting_id} with platform/native validation")
    else:
        # Get latest meeting by platform/native_meeting_id (default behavior)
        stmt_meeting = (
            select(Meeting)
            .where(
                Meeting.account_id == account.id,
                Meeting.platform == platform.value,
                Meeting.platform_specific_id == native_meeting_id,
            )
            .order_by(Meeting.created_at.desc())
        )
        logger.debug(f"[API] Looking for latest meeting for platform/native_id")

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()

    if not meeting:
        if meeting_id is not None:
            logger.warning(
                f"[API] No meeting found for account {account.id}, platform '{platform.value}', native ID '{native_meeting_id}', meeting_id '{meeting_id}'"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting not found for platform {platform.value}, ID {native_meeting_id}, and meeting_id {meeting_id}",
            )
        else:
            logger.warning(
                f"[API] No meeting found for account {account.id}, platform '{platform.value}', native ID '{native_meeting_id}'"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}",
            )

    internal_meeting_id = meeting.id
    logger.debug(f"[API] Found meeting record ID {internal_meeting_id}, fetching segments...")

    sorted_segments = await _get_full_transcript_segments(internal_meeting_id, db, redis_c)

    logger.info(f"[API Meet {internal_meeting_id}] Merged and sorted into {len(sorted_segments)} total segments.")

    meeting_details = MeetingResponse.model_validate(meeting)
    response_data = meeting_details.dict()
    response_data["segments"] = sorted_segments
    return TranscriptionResponse(**response_data)


@router.post(
    "/ws/authorize-subscribe",
    response_model=WsAuthorizeSubscribeResponse,
    summary="Authorize WS subscription for meetings",
    description="Validates that the authenticated account is allowed to subscribe to the given meetings and that identifiers are valid.",
    dependencies=[Depends(get_account_from_api_key)],
)
async def ws_authorize_subscribe(
    payload: WsAuthorizeSubscribeRequest,
    account: Account = Depends(get_account_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    authorized: List[Dict[str, str]] = []
    errors: List[str] = []

    meetings = payload.meetings or []
    if not meetings:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'meetings' must be a non-empty list",
        )

    for idx, meeting_ref in enumerate(meetings):
        platform_value = (
            meeting_ref.platform.value if isinstance(meeting_ref.platform, Platform) else str(meeting_ref.platform)
        )
        native_id = meeting_ref.native_meeting_id

        # Validate platform/native ID format via construct_meeting_url
        try:
            constructed = Platform.construct_meeting_url(platform_value, native_id)
        except Exception:
            constructed = None
        if not constructed:
            errors.append(f"meetings[{idx}] invalid native_meeting_id for platform '{platform_value}'")
            continue

        stmt_meeting = (
            select(Meeting)
            .where(
                Meeting.account_id == account.id,
                Meeting.platform == platform_value,
                Meeting.platform_specific_id == native_id,
            )
            .order_by(Meeting.created_at.desc())
            .limit(1)
        )

        result = await db.execute(stmt_meeting)
        meeting = result.scalars().first()
        if not meeting:
            errors.append(f"meetings[{idx}] not authorized or not found for account")
            continue

        authorized.append(
            {
                "platform": platform_value,
                "native_id": native_id,
                "account_id": str(account.id),
                "meeting_id": str(meeting.id),
            }
        )

    return WsAuthorizeSubscribeResponse(authorized=authorized, errors=errors, account_id=account.id)


@router.get(
    "/internal/transcripts/{meeting_id}",
    response_model=List[TranscriptionSegment],
    summary="[Internal] Get all transcript segments for a meeting",
    include_in_schema=False,
)
async def get_transcript_internal(meeting_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Internal endpoint for services to fetch all transcript segments for a given meeting ID."""
    logger.debug(f"[Internal API] Transcript segments requested for meeting {meeting_id}")
    redis_c = getattr(request.app.state, "redis_client", None)

    meeting = await db.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting with ID {meeting_id} not found.",
        )

    segments = await _get_full_transcript_segments(meeting_id, db, redis_c)
    return segments


@router.patch(
    "/meetings/{platform}/{native_meeting_id}",
    response_model=MeetingResponse,
    summary="Update meeting data by platform and native ID",
    dependencies=[Depends(get_account_from_api_key)],
)
async def update_meeting_data(
    platform: Platform,
    native_meeting_id: str,
    meeting_update: MeetingUpdate,
    account: Account = Depends(get_account_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Updates the user-editable data (name, participants, languages, notes) for the latest meeting matching the platform and native ID."""

    logger.info(f"[API] Account {account.id} updating meeting {platform.value}/{native_meeting_id}")
    logger.debug(f"[API] Raw meeting_update object: {meeting_update}")
    logger.debug(f"[API] meeting_update.data type: {type(meeting_update.data)}")
    logger.debug(f"[API] meeting_update.data content: {meeting_update.data}")

    stmt = (
        select(Meeting)
        .where(
            Meeting.account_id == account.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id,
        )
        .order_by(Meeting.created_at.desc())
    )

    result = await db.execute(stmt)
    meeting = result.scalars().first()

    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}",
        )

    # Extract update data from the MeetingDataUpdate object
    try:
        if hasattr(meeting_update.data, "dict"):
            # meeting_update.data is a MeetingDataUpdate pydantic object
            update_data = meeting_update.data.dict(exclude_unset=True)
            logger.debug(f"[API] Extracted update_data via .dict(): {update_data}")
        else:
            # Fallback: meeting_update.data is already a dict
            update_data = meeting_update.data
            logger.debug(f"[API] Using update_data as dict: {update_data}")
    except AttributeError:
        # Handle case where data might be parsed differently
        update_data = meeting_update.data
        logger.debug(f"[API] Fallback update_data: {update_data}")

    # Remove None values from update_data
    update_data = {k: v for k, v in update_data.items() if v is not None}
    logger.debug(f"[API] Final update_data after filtering None values: {update_data}")

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No data provided for update.",
        )

    if meeting.data is None:
        meeting.data = {}
        logger.debug(f"[API] Initialized empty meeting.data")

    logger.debug(f"[API] Current meeting.data before update: {meeting.data}")

    # Only allow updating restricted fields: name, participants, languages, notes
    allowed_fields = {"name", "participants", "languages", "notes"}
    updated_fields = []

    # Create a new copy of the data dict to ensure SQLAlchemy detects the change
    new_data = dict(meeting.data) if meeting.data else {}

    for key, value in update_data.items():
        if key in allowed_fields and value is not None:
            new_data[key] = value
            updated_fields.append(f"{key}={value}")
            logger.debug(f"[API] Updated field {key} = {value}")
        else:
            logger.debug(f"[API] Skipped field {key} (not in allowed_fields or value is None)")

    # Assign the new dict to ensure SQLAlchemy detects the change
    meeting.data = new_data

    # Mark the field as modified to ensure SQLAlchemy detects the change
    from sqlalchemy.orm import attributes

    attributes.flag_modified(meeting, "data")

    logger.info(f"[API] Updated fields: {', '.join(updated_fields) if updated_fields else 'none'}")
    logger.debug(f"[API] Final meeting.data after update: {meeting.data}")

    await db.commit()
    await db.refresh(meeting)

    logger.debug(f"[API] Meeting.data after commit and refresh: {meeting.data}")

    return MeetingResponse.model_validate(meeting)


@router.delete(
    "/meetings/{platform}/{native_meeting_id}",
    summary="Delete meeting transcripts and anonymize meeting data",
    dependencies=[Depends(get_account_from_api_key)],
)
async def delete_meeting(
    platform: Platform,
    native_meeting_id: str,
    request: Request,
    account: Account = Depends(get_account_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Purges transcripts and anonymizes meeting data for finalized meetings.

    Only allows deletion for meetings in finalized states (completed, failed).
    Deletes all transcripts but preserves meeting and session records for telemetry.
    Scrubs PII from meeting record while keeping telemetry data.
    """

    stmt = (
        select(Meeting)
        .where(
            Meeting.account_id == account.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id,
        )
        .order_by(Meeting.created_at.desc())
    )

    result = await db.execute(stmt)
    meeting = result.scalars().first()

    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}",
        )

    internal_meeting_id = meeting.id

    # Check if already redacted (idempotency)
    if meeting.data and meeting.data.get("redacted"):
        logger.info(f"[API] Meeting {internal_meeting_id} already redacted, returning success")
        return {
            "message": f"Meeting {platform.value}/{native_meeting_id} transcripts already deleted and data anonymized"
        }

    # Check if meeting is in finalized state
    finalized_states = {MeetingStatus.COMPLETED.value, MeetingStatus.FAILED.value}
    if meeting.status not in finalized_states:
        logger.warning(
            f"[API] Account {account.id} attempted to delete non-finalized meeting {internal_meeting_id} (status: {meeting.status})"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Meeting not finalized; cannot delete transcripts. Current status: {meeting.status}",
        )

    logger.info(f"[API] Account {account.id} purging transcripts and anonymizing meeting {internal_meeting_id}")

    # Delete transcripts from PostgreSQL
    stmt_transcripts = select(Transcription).where(Transcription.meeting_id == internal_meeting_id)
    result_transcripts = await db.execute(stmt_transcripts)
    transcripts = result_transcripts.scalars().all()

    for transcript in transcripts:
        await db.delete(transcript)

    # Delete transcript segments from Redis and remove from active meetings
    redis_c = getattr(request.app.state, "redis_client", None)
    if redis_c:
        try:
            hash_key = f"meeting:{internal_meeting_id}:segments"
            # Use pipeline for atomic operations
            async with redis_c.pipeline(transaction=True) as pipe:
                pipe.delete(hash_key)
                pipe.srem("active_meetings", str(internal_meeting_id))
                results = await pipe.execute()
            logger.debug(f"[API] Deleted Redis hash {hash_key} and removed from active_meetings")
        except Exception as e:
            logger.error(f"[API] Failed to delete Redis data for meeting {internal_meeting_id}: {e}")

    # Scrub PII from meeting record while preserving telemetry
    original_data = meeting.data or {}

    # Keep only telemetry fields
    telemetry_fields = {
        "status_transition",
        "completion_reason",
        "error",
        "diagnostics",
    }
    scrubbed_data = {k: v for k, v in original_data.items() if k in telemetry_fields}

    # Add redaction marker for idempotency
    scrubbed_data["redacted"] = True

    # Update meeting record with scrubbed data
    meeting.platform_specific_id = None  # Clear native meeting ID (this makes constructed_meeting_url return None)
    meeting.data = scrubbed_data

    # Note: We keep Meeting and MeetingSession records for telemetry
    await db.commit()

    logger.info(f"[API] Successfully purged transcripts and anonymized meeting {internal_meeting_id}")

    return {"message": f"Meeting {platform.value}/{native_meeting_id} transcripts deleted and data anonymized"}


# ============================================================================
# Cloudflare Whisper Proxy Ingestion Endpoint
# ============================================================================


class CFProxyTranscriptionSegment(BaseModel):
    """Segment from Cloudflare Workers AI transcription"""

    start: float
    end: float
    text: str
    temperature: Optional[float] = None
    avg_logprob: Optional[float] = None
    compression_ratio: Optional[float] = None
    no_speech_prob: Optional[float] = None


class CFProxyTranscriptionRequest(BaseModel):
    """Request from Cloudflare Whisper Proxy"""

    session_id: str
    meeting_id: Optional[int] = None
    audio_key: Optional[str] = None  # R2 key for idempotent storage
    chunk_index: int
    timestamp: int  # Unix timestamp ms
    text: str
    segments: Optional[List[CFProxyTranscriptionSegment]] = None
    language: Optional[str] = None
    language_probability: Optional[float] = None
    duration: Optional[float] = None
    speaker: Optional[str] = None  # Speaker name from meeting UI


@router.post(
    "/transcripts/webhook",
    summary="Ingest transcription from Cloudflare Whisper Proxy",
    description="Receives batched transcription results from the Cloudflare Workers AI proxy",
)
async def ingest_cf_proxy_transcription(
    request: CFProxyTranscriptionRequest,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    """
    Endpoint for Cloudflare Whisper Proxy to submit transcriptions.
    This processes batch transcription results and stores them.
    Requires a valid MeetingToken in the Authorization header.
    """
    # Extract token from Bearer format
    token = None
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]  # Remove 'Bearer ' prefix
        else:
            token = authorization

    # Verify MeetingToken from Authorization header
    if not token:
        logger.warning(f"[CF-Proxy] Missing Authorization header for session {request.session_id}")
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    claims = verify_meeting_token(token)
    if not claims:
        logger.warning(f"[CF-Proxy] Invalid MeetingToken for session {request.session_id}")
        raise HTTPException(status_code=401, detail="Invalid or expired MeetingToken")

    # Verify meeting_id in request matches token claims
    if request.meeting_id and claims.get("meeting_id") != request.meeting_id:
        logger.warning(
            f"[CF-Proxy] Meeting ID mismatch: request={request.meeting_id}, token={claims.get('meeting_id')}"
        )
        raise HTTPException(status_code=403, detail="Meeting ID mismatch")

    logger.info(f"[CF-Proxy] Received transcription for session {request.session_id}, chunk {request.chunk_index}")

    if not request.text or not request.text.strip():
        logger.debug(f"[CF-Proxy] Empty transcription for chunk {request.chunk_index}, skipping")
        return {"status": "skipped", "reason": "empty_transcription"}

    # Try to find meeting by session_id (stored in MeetingSession)
    meeting = None
    session = None

    if request.meeting_id:
        # Look up meeting by internal ID
        stmt = select(Meeting).where(Meeting.id == request.meeting_id)
        result = await db.execute(stmt)
        meeting = result.scalar_one_or_none()

    if not meeting:
        # Try to find by session_uid
        stmt = select(MeetingSession).where(MeetingSession.session_uid == request.session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if session:
            stmt = select(Meeting).where(Meeting.id == session.meeting_id)
            result = await db.execute(stmt)
            meeting = result.scalar_one_or_none()

    if not meeting:
        logger.warning(f"[CF-Proxy] No meeting found for session {request.session_id}")
        return {"status": "error", "reason": "meeting_not_found"}

    # Build segments JSONB for storage
    segments_json = None
    if request.segments:
        segments_json = [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "temperature": seg.temperature,
                "avg_logprob": seg.avg_logprob,
                "compression_ratio": seg.compression_ratio,
                "no_speech_prob": seg.no_speech_prob,
            }
            for seg in request.segments
            if seg.text and seg.text.strip()
        ]

    # Use audio_key for storage, fallback to session_id+chunk_index
    audio_key = request.audio_key or f"{request.session_id}/{request.timestamp}-{request.chunk_index}.raw"

    # Store all chunks - deduplication handled later during AI processing
    # Create new AudioChunk record
    audio_chunk = AudioChunk(
        meeting_id=meeting.id,
        session_uid=request.session_id,
        audio_key=audio_key,
        chunk_index=request.chunk_index,
        chunk_timestamp=request.timestamp,
        duration=request.duration,
        full_text=request.text.strip() if request.text else None,
        segments=segments_json,
        language=request.language,
        language_probability=request.language_probability,
        speaker=request.speaker,
    )
    db.add(audio_chunk)
    await db.commit()
    await db.refresh(audio_chunk)

    logger.info(f"[CF-Proxy] Stored chunk {request.chunk_index} for meeting {meeting.id} (audio_key: {audio_key})")

    return {
        "status": "success",
        "meeting_id": meeting.id,
        "chunk_id": audio_chunk.id,
        "audio_key": audio_key,
        "language": request.language,
    }
