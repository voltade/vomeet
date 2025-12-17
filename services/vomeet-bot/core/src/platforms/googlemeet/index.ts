import type { Page } from "playwright";
import type { BotConfig } from "../../types";
import { type PlatformStrategies, runMeetingFlow } from "../shared/meetingFlow";
import { waitForGoogleMeetingAdmission } from "./admission";
// Import modular functions
import { joinGoogleMeeting } from "./join";
import { leaveGoogleMeet, prepareForRecording } from "./leave";
import { startGoogleRecording } from "./recording";
import { startGoogleRemovalMonitor } from "./removal";

// --- Google Meet Main Handler ---

export async function handleGoogleMeet(
	botConfig: BotConfig,
	page: Page,
	gracefulLeaveFunction: (
		page: Page | null,
		exitCode: number,
		reason: string,
		errorDetails?: any,
	) => Promise<void>,
): Promise<void> {
	const strategies: PlatformStrategies = {
		join: async (page: Page, botConfig: BotConfig) => {
			await joinGoogleMeeting(
				page,
				botConfig.meetingUrl!,
				botConfig.botName,
				botConfig,
			);
		},
		waitForAdmission: waitForGoogleMeetingAdmission,
		prepare: prepareForRecording,
		startRecording: startGoogleRecording,
		startRemovalMonitor: startGoogleRemovalMonitor,
		leave: leaveGoogleMeet,
	};

	await runMeetingFlow(
		"google_meet",
		botConfig,
		page,
		gracefulLeaveFunction,
		strategies,
	);
}

// Export the leave function for external use
export { leaveGoogleMeet };
