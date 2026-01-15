export type BotConfig = {
	platform: "google_meet" | "zoom" | "teams";
	meetingUrl: string | null;
	botName: string;
	token: string; // MeetingToken (HS256 JWT)
	connectionId: string;
	nativeMeetingId: string;
	language?: string | null;
	task?: string | null;
	redisUrl: string;
	container_name?: string;
	automaticLeave: {
		waitingRoomTimeout: number;
		noOneJoinedTimeout: number;
		everyoneLeftTimeout: number;
		/** Idle timeout after scheduled end time in milliseconds (default: 15 minutes = 900000ms) */
		idleAfterScheduledEndTimeout?: number;
	};
	reconnectionIntervalMs?: number;
	meeting_id: number; // Required, not optional
	botManagerCallbackUrl?: string;
	/** Scheduled start time of the meeting as ISO 8601 string or Unix timestamp in milliseconds */
	scheduledStartTime?: string | number | null;
	/** Scheduled end time of the meeting as ISO 8601 string or Unix timestamp in milliseconds */
	scheduledEndTime?: string | number | null;
};
