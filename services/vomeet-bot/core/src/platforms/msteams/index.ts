import type { Page } from "playwright";
import type { BotConfig } from "../../types";
import { type PlatformStrategies, runMeetingFlow } from "../shared/meetingFlow";
import { waitForTeamsMeetingAdmission } from "./admission";
// Import modular functions
import { joinMicrosoftTeams } from "./join";
import { leaveMicrosoftTeams, prepareForRecording } from "./leave";
import { startTeamsRecording } from "./recording";
import { startTeamsRemovalMonitor } from "./removal";

export async function handleMicrosoftTeams(
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
		join: joinMicrosoftTeams,
		waitForAdmission: waitForTeamsMeetingAdmission,
		prepare: prepareForRecording,
		startRecording: startTeamsRecording,
		startRemovalMonitor: startTeamsRemovalMonitor,
		leave: leaveMicrosoftTeams,
	};

	await runMeetingFlow(
		"teams",
		botConfig,
		page,
		gracefulLeaveFunction,
		strategies,
	);
}

// Export the leave function for external use
export { leaveMicrosoftTeams };
