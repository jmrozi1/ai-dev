import * as vscode from 'vscode';
import * as fs from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import * as path from 'node:path';
import { registerAiDevActionsView } from './actionsView';
import {
	fileExists,
	readOptionalTextFile,
	truncateText,
} from './fileUtilities';
import {
	ARCHITECTURE_SUMMARY_FILE_NAME,
	AiDevConfig,
	getAiDevYamlPromptSection,
	getArchitectureSummaryPath,
	getExecutionModeFromConfig,
	getLegacyRootSummaryFilePath,
	getRootSummaryFilePath,
	readAiDevConfig,
	resolveAiDevCorePath,
	setAiDevExtensionRootPath,
} from './config';
import {
	existingChangedFilesWithContent,
	getGitDiffForFiles,
	getGitChangedFiles,
	getGitRenameRecords,
	type GitFileDiff,
	type GitRenameRecord,
} from './git';
import {
	buildAnswerPromptMarkdown,
	buildAnswerFromAiDocsDirectPromptMarkdown,
	buildGenerateArchitectureSummaryDirectPromptMarkdown,
	buildGroupedGenerateUnitDocDirectPromptMarkdown,
	buildUnitDocPromptMarkdown,
} from './promptBuilder';
import {
	MAX_DIRECT_FILE_CHARS,
} from './modelContextLimits';
import {
	NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS,
	globToRegExp,
	matchesAnyGlob,
} from './pathMatching';
import {
	discoverBatchUnitDocCandidates,
	getBatchSourceGlobs,
	getConfiguredDocsDir,
	isConfiguredSourceCandidatePath,
	isPathInsideDirectory,
	normalizeBatchSourceGlob,
} from './sourceDiscovery';
import {
	getActiveWorkspaceContext,
	getExpectedDirectorySummaryPath,
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
	getSelectedSourcePath,
} from './workspace';
import {
	DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS,
	MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
	type BatchUnitDocActionType,
	type BatchUnitDocPreviewItem,
	type BatchUnitDocPreviewResult,
	type BatchUnitDocStatus,
	type BatchUnitDocsFormState,
} from './batchUnitDocs';
import {
	type ArchitectureSummaryPreviewItem,
	type ArchitectureSummaryPreviewResult,
	type ArchitectureSummaryStatus,
} from './architectureSummary';
import {
	prepareProjectReview,
	previewProjectReview,
} from './projectReview';
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
	AiDevAssistantTerminalManager,
	type AssistantReviewRequest,
} from './assistantTerminal';
import { AssistantReportStore } from './assistantReport';
import { AssistantReportPanel } from './assistantReportPanel';
import {
	injectSummarizationInstructions,
	readSummarizationConfig,
	resolveSummarizationInstructions,
	writeSummarizationConfig,
} from './summarizationConfig';
import {
	SummarizationConfigPanel,
} from './summarizationConfigPanel';
import {
	openSettingsCommand,
} from './settingsWorkflow';
import {
	completeWorkspacePath,
} from './workspacePathCompletion';

const SETTINGS_COMMAND = 'aiDev.settings';
const LAUNCH_ASSISTANT_COMMAND = 'aiDev.launchAssistant';
const FALLBACK_SUMMARY_FILE = 'ai-docs/summary.md';
const MAX_DIRECT_INDEX_UNIT_DOCS = 20;
function extractFirstNonMetaDiffHunk(diff: string): string | undefined {
	const lines = diff.split(/\r?\n/);
	const hunkLines = lines.filter((line) => line.startsWith('@@') || line.startsWith('+') || line.startsWith('-'));
	if (hunkLines.length === 0) {
		return undefined;
	}

	return truncateText(hunkLines.join('\n').trim(), 500);
}

function isLikelyCommentOnlyDiff(diff: string): boolean {
	const lines = diff.split(/\r?\n/);
	const changedLines = lines.filter((line) => {
		if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) {
			return false;
		}

		return line.startsWith('+') || line.startsWith('-');
	});

	if (changedLines.length === 0) {
		return false;
	}

	for (const line of changedLines) {
		const content = line.slice(1).trim();
		if (content.length === 0) {
			continue;
		}

		const isComment = content.startsWith('--')
			|| content.startsWith('//')
			|| content.startsWith('#')
			|| content.startsWith('/*')
			|| content.startsWith('*')
			|| content.startsWith('*/');

		if (!isComment) {
			return false;
		}
	}

	return true;
}

function isTestFilePath(filePath: string): boolean {
	const normalized = normalizePathForMarkdown(filePath).toLowerCase();

	return (
		normalized.includes('/test/')
		|| normalized.includes('/tests/')
		|| normalized.includes('/__tests__/')
		|| normalized.includes('.test.')
		|| normalized.includes('.spec.')
	);
}


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
