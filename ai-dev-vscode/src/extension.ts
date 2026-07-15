import * as path from 'node:path';
import * as vscode from 'vscode';
import { registerAiDevActionsView } from './actionsView';
import {
	AiDevAssistantTerminalManager,
} from './assistantTerminal';
import { AssistantReportPanel } from './assistantReportPanel';
import { AssistantReportStore } from './assistantReport';
import {
	readAiDevConfig,
	setAiDevExtensionRootPath,
} from './config';
import {
	globToRegExp,
} from './pathMatching';
import {
	prepareProjectReview,
	previewProjectReview,
} from './projectReview';
import {
	discoverBatchUnitDocCandidates,
} from './sourceDiscovery';
import {
	openSettingsCommand,
} from './settingsWorkflow';
import {
	readSummarizationConfig,
	writeSummarizationConfig,
} from './summarizationConfig';
import {
	SummarizationConfigPanel,
} from './summarizationConfigPanel';
import {
	completeSingleFileSummary,
	executeSummarizationTarget,
	prepareSingleFileSummary,
	previewSummarizationTarget,
} from './summarizationWorkflow';
import {
	buildSummaryAnswerRoute,
} from './summaryAnswerRouting';
import {
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
} from './workspace';
import {
	completeWorkspacePath,
} from './workspacePathCompletion';

const SETTINGS_COMMAND = 'aiDev.settings';
const LAUNCH_ASSISTANT_COMMAND = 'aiDev.launchAssistant';

export function activate(context: vscode.ExtensionContext) {
	setAiDevExtensionRootPath(context.extensionPath);
	registerAiDevActionsView(context);

	const assistantReportStore = new AssistantReportStore();
	const assistantReportPanel = new AssistantReportPanel();
	const summarizationConfigPanel =
		new SummarizationConfigPanel({
			load: async () => {
				const workspaceRoot = getOpenWorkspaceRoot();

				if (!workspaceRoot) {
					throw new Error('No workspace is open.');
				}

				return readSummarizationConfig(workspaceRoot);
			},
			save: async (config) => {
				const workspaceRoot = getOpenWorkspaceRoot();

				if (!workspaceRoot) {
					throw new Error('No workspace is open.');
				}

				await writeSummarizationConfig(
					workspaceRoot,
					config
				);
			},
			testPattern: async (glob) => {
				const workspaceRoot = getOpenWorkspaceRoot();

				if (!workspaceRoot) {
					throw new Error('No workspace is open.');
				}

				const aiDevConfig =
					await readAiDevConfig(workspaceRoot);
				const candidates =
					await discoverBatchUnitDocCandidates(
						workspaceRoot,
						aiDevConfig
					);

				const matcher = globToRegExp(glob);
				const matchingPaths = candidates
					.map((absolutePath) =>
						normalizePathForMarkdown(
							path.relative(
								workspaceRoot,
								absolutePath
							)
						)
					)
					.filter((relativePath) =>
						matcher.test(relativePath)
					)
					.sort((left, right) =>
						left.localeCompare(right)
					);

				return {
					totalMatches: matchingPaths.length,
					previewPaths: matchingPaths.slice(0, 10),
					omittedCount:
						Math.max(0, matchingPaths.length - 10),
				};
			},
		});

	context.subscriptions.push(
		assistantReportPanel,
		summarizationConfigPanel
	);

	const assistantTerminalManager = new AiDevAssistantTerminalManager(
		vscode.window,
		undefined,
		buildSummaryAnswerRoute,
		{
			preview: previewSummarizationTarget,
			execute: executeSummarizationTarget,
			prepare: prepareSingleFileSummary,
			complete: completeSingleFileSummary,
		},
		{
			preview: previewProjectReview,
			prepare: prepareProjectReview,
		},
		(report) => {
			assistantReportStore.setLatest(report);
			assistantReportPanel.refresh();
		},
		() => {
			const report = assistantReportStore.getLatest();
			if (!report) {
				return false;
			}

			assistantReportPanel.show(report);
			return true;
		},
		async () => {
			await summarizationConfigPanel.show();
		},
		completeWorkspacePath
	);

	context.subscriptions.push(assistantTerminalManager);

	const launchAssistantCommand = vscode.commands.registerCommand(
		LAUNCH_ASSISTANT_COMMAND,
		() => {
			assistantTerminalManager.launchAssistant();
		}
	);




	const settingsCommand = vscode.commands.registerCommand(
		SETTINGS_COMMAND,
		() => openSettingsCommand(context)
	);


	context.subscriptions.push(
		launchAssistantCommand,
		settingsCommand
	);
}

export function deactivate() {}
