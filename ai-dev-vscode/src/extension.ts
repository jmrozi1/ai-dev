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
	type AiProviderMode,
	getAiDevYamlPromptSection,
	getArchitectureSummaryPath,
	getExecutionModeFromConfig,
	getLegacyRootSummaryFilePath,
	getRootSummaryFilePath,
	getYamlNestedValue,
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
	FALLBACK_BATCH_EXCLUDE_GLOBS,
	discoverBatchUnitDocCandidates,
	getBatchSourceGlobs,
	getConfiguredDocsDir,
	isConfiguredSourceCandidatePath,
	isPathInsideDirectory,
	normalizeBatchSourceGlob,
	parseYamlList,
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
import { openSettingsWebview } from './settingsView';
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

const SETTINGS_COMMAND = 'aiDev.settings';
const LAUNCH_ASSISTANT_COMMAND = 'aiDev.launchAssistant';
const FALLBACK_SUMMARY_FILE = 'ai-docs/summary.md';
const MAX_DIRECT_INDEX_UNIT_DOCS = 20;
function getIndentation(line: string): string {
	const match = line.match(/^[ \t]*/);
	return match ? match[0] : '';
}

function withOriginalLineEnding(original: string, updated: string): string {
	const lineEnding = original.includes('\r\n') ? '\r\n' : '\n';
	const normalizedUpdated = updated.replace(/\n/g, lineEnding);
	if (original.endsWith('\n') || original.endsWith('\r\n')) {
		return normalizedUpdated.endsWith(lineEnding) ? normalizedUpdated : `${normalizedUpdated}${lineEnding}`;
	}

	return normalizedUpdated.endsWith(lineEnding)
		? normalizedUpdated.slice(0, -lineEnding.length)
		: normalizedUpdated;
}

function updateAiProviderModeInYaml(yamlContent: string, selectedMode: AiProviderMode): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const modeValue = `"${selectedMode}"`;
	const aiProviderLinePattern = /^\s*aiProvider\s*:/;
	const modeLinePattern = /^\s*mode\s*:/;

	const aiProviderIndex = lines.findIndex((line) => aiProviderLinePattern.test(line));

	if (aiProviderIndex >= 0) {
		const aiProviderIndent = getIndentation(lines[aiProviderIndex]).length;
		let blockEnd = lines.length;
		for (let index = aiProviderIndex + 1; index < lines.length; index += 1) {
			const line = lines[index];
			const trimmed = line.trim();
			if (trimmed.length === 0 || trimmed.startsWith('#')) {
				continue;
			}

			const indent = getIndentation(line).length;
			if (indent <= aiProviderIndent) {
				blockEnd = index;
				break;
			}
		}

		for (let index = aiProviderIndex + 1; index < blockEnd; index += 1) {
			if (!modeLinePattern.test(lines[index])) {
				continue;
			}

			const modeIndentation = getIndentation(lines[index]);
			lines[index] = `${modeIndentation}mode: ${modeValue}`;
			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const childIndentation = (() => {
			for (let index = aiProviderIndex + 1; index < blockEnd; index += 1) {
				const line = lines[index];
				const trimmed = line.trim();
				if (trimmed.length === 0 || trimmed.startsWith('#')) {
					continue;
				}

				const indentation = getIndentation(line);
				if (indentation.length > aiProviderIndent) {
					return indentation;
				}
			}

			return `${getIndentation(lines[aiProviderIndex])}  `;
		})();

		lines.splice(aiProviderIndex + 1, 0, `${childIndentation}mode: ${modeValue}`);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const base = normalized.trimEnd();
	const suffix = base.length > 0 ? '\n\naiProvider:\n  mode: ' : 'aiProvider:\n  mode: ';
	const updated = `${base}${suffix}${modeValue}`;
	return withOriginalLineEnding(yamlContent, updated);
}

function quoteYamlString(value: string): string {
	const escaped = value
		.replace(/\\/g, '\\\\')
		.replace(/"/g, '\\"');
	return `"${escaped}"`;
}

function getTopLevelSectionBounds(lines: string[], section: string): {
	sectionIndex: number;
	sectionIndent: number;
	blockEnd: number;
} | undefined {
	const sectionLinePattern = new RegExp(`^\\s*${section}\\s*:\\s*$`);
	const sectionIndex = lines.findIndex((line) => sectionLinePattern.test(line));
	if (sectionIndex < 0) {
		return undefined;
	}

	const sectionIndent = getIndentation(lines[sectionIndex]).length;
	let blockEnd = lines.length;
	for (let index = sectionIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		const trimmed = line.trim();
		if (trimmed.length === 0 || trimmed.startsWith('#')) {
			continue;
		}

		const indent = getIndentation(line).length;
		if (indent <= sectionIndent) {
			blockEnd = index;
			break;
		}
	}

	return { sectionIndex, sectionIndent, blockEnd };
}

function updateYamlSectionScalarValue(yamlContent: string, section: string, key: string, value: string): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const quotedValue = quoteYamlString(value);
	const bounds = getTopLevelSectionBounds(lines, section);

	if (bounds) {
		const { sectionIndex, sectionIndent, blockEnd } = bounds;
		const keyPattern = new RegExp(`^\\s{${sectionIndent + 2}}${key}\\s*:`);

		for (let index = sectionIndex + 1; index < blockEnd; index += 1) {
			if (!keyPattern.test(lines[index])) {
				continue;
			}

			const keyIndentation = getIndentation(lines[index]);
			const keyIndentLength = keyIndentation.length;
			lines[index] = `${keyIndentation}${key}: ${quotedValue}`;

			let removalEnd = index + 1;
			while (removalEnd < blockEnd) {
				const childLine = lines[removalEnd];
				const childTrimmed = childLine.trim();
				if (childTrimmed.length === 0) {
					removalEnd += 1;
					continue;
				}

				if (childTrimmed.startsWith('#')) {
					removalEnd += 1;
					continue;
				}

				const childIndent = getIndentation(childLine).length;
				if (childIndent <= keyIndentLength) {
					break;
				}

				removalEnd += 1;
			}

			if (removalEnd > index + 1) {
				lines.splice(index + 1, removalEnd - (index + 1));
			}

			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const childIndentation = `${getIndentation(lines[sectionIndex])}  `;
		lines.splice(sectionIndex + 1, 0, `${childIndentation}${key}: ${quotedValue}`);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const base = normalized.trimEnd();
	const suffix = base.length > 0
		? `\n\n${section}:\n  ${key}: ${quotedValue}`
		: `${section}:\n  ${key}: ${quotedValue}`;
	return withOriginalLineEnding(yamlContent, `${base}${suffix}`);
}

function updateYamlSectionListValue(yamlContent: string, section: string, key: string, values: string[]): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const bounds = getTopLevelSectionBounds(lines, section);

	if (bounds) {
		const { sectionIndex, sectionIndent, blockEnd } = bounds;
		const keyPattern = new RegExp(`^\\s{${sectionIndent + 2}}${key}\\s*:`);
		const keyIndentation = `${getIndentation(lines[sectionIndex])}  `;
		const itemIndentation = `${keyIndentation}  `;

		for (let index = sectionIndex + 1; index < blockEnd; index += 1) {
			if (!keyPattern.test(lines[index])) {
				continue;
			}

			const existingKeyIndentation = getIndentation(lines[index]);
			const keyIndentLength = existingKeyIndentation.length;
			lines[index] = `${existingKeyIndentation}${key}:`;

			let blockRemovalEnd = index + 1;
			while (blockRemovalEnd < blockEnd) {
				const candidate = lines[blockRemovalEnd];
				const trimmed = candidate.trim();
				if (trimmed.length === 0) {
					blockRemovalEnd += 1;
					continue;
				}

				if (trimmed.startsWith('#')) {
					blockRemovalEnd += 1;
					continue;
				}

				const candidateIndent = getIndentation(candidate).length;
				if (candidateIndent <= keyIndentLength) {
					break;
				}

				blockRemovalEnd += 1;
			}

			if (blockRemovalEnd > index + 1) {
				lines.splice(index + 1, blockRemovalEnd - (index + 1));
			}

			const itemLines = values.map((value) => `${itemIndentation}- ${quoteYamlString(value)}`);
			if (itemLines.length > 0) {
				lines.splice(index + 1, 0, ...itemLines);
			}

			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const insertionLines = [
			`${keyIndentation}${key}:`,
			...values.map((value) => `${itemIndentation}- ${quoteYamlString(value)}`),
		];
		lines.splice(sectionIndex + 1, 0, ...insertionLines);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const listLines = [
		`${section}:`,
		`  ${key}:`,
		...values.map((value) => `    - ${quoteYamlString(value)}`),
	];
	const base = normalized.trimEnd();
	const suffix = base.length > 0 ? `\n\n${listLines.join('\n')}` : listLines.join('\n');
	return withOriginalLineEnding(yamlContent, `${base}${suffix}`);
}

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

const PATH_COMPLETION_EXCLUDED_NAMES = new Set([
	'.git',
	'node_modules',
	'out',
	'dist',
	'build',
	'coverage',
	'.vscode-test',
]);

async function completeWorkspacePath(
	partialPath: string
): Promise<{ matches: string[] }> {
	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		return { matches: [] };
	}

	const normalizedPartial =
		normalizePathForMarkdown(partialPath);
	const slashIndex = normalizedPartial.lastIndexOf('/');

	const directoryPart =
		slashIndex >= 0
			? normalizedPartial.slice(0, slashIndex + 1)
			: '';

	const namePrefix =
		slashIndex >= 0
			? normalizedPartial.slice(slashIndex + 1)
			: normalizedPartial;

	const directoryAbsolutePath = path.resolve(
		workspaceRoot,
		directoryPart || '.'
	);

	if (
		!isPathInsideDirectory(
			directoryAbsolutePath,
			workspaceRoot
		)
	) {
		return { matches: [] };
	}

	let entries;

	try {
		entries = await fs.readdir(
			directoryAbsolutePath,
			{ withFileTypes: true }
		);
	} catch {
		return { matches: [] };
	}

	const matches = entries
		.filter(
			(entry) =>
				!PATH_COMPLETION_EXCLUDED_NAMES.has(entry.name)
				&& entry.name.startsWith(namePrefix)
		)
		.map((entry) =>
			`${directoryPart}${entry.name}`
			+ (entry.isDirectory() ? '/' : '')
		)
		.sort((left, right) =>
			left.localeCompare(right)
		);

	return { matches };
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



	const openSettingsCommand = async (): Promise<void> => {
		const workspaceRoot = getOpenWorkspaceRoot();
		if (!workspaceRoot) {
			await vscode.window.showErrorMessage('No workspace is open.');
			return;
		}

		const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');
		let initialPromptOnly = false;
		let initialDocsDir = 'ai-docs';
		let initialBatchInitialSourceGlob = '**/*';
		let initialSourceExcludeGlobs = [...FALLBACK_BATCH_EXCLUDE_GLOBS];
		try {
			const aiDevYamlContents = await fs.readFile(aiDevYamlPath, 'utf8');
			initialPromptOnly = getYamlNestedValue(aiDevYamlContents, 'aiProvider', 'mode')?.trim() === 'prompt-only';
			const configuredDocsDir = getYamlNestedValue(aiDevYamlContents, 'documentation', 'docsDir')?.trim();
			if (configuredDocsDir) {
				initialDocsDir = configuredDocsDir;
			}

			const configuredBatchInitialSourceGlob = getYamlNestedValue(aiDevYamlContents, 'documentation', 'batchInitialSourceGlob')?.trim();
			if (configuredBatchInitialSourceGlob) {
				initialBatchInitialSourceGlob = configuredBatchInitialSourceGlob;
			}

			const configuredSourceExcludeGlobs = parseYamlList(aiDevYamlContents, 'source', 'exclude');
			if (configuredSourceExcludeGlobs.length > 0) {
				initialSourceExcludeGlobs = configuredSourceExcludeGlobs;
			}
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
				const message = error instanceof Error ? error.message : String(error);
				await vscode.window.showErrorMessage(`Failed to read .ai-dev.yaml: ${message}`);
				return;
			}
		}

		openSettingsWebview(context, {
			initialPromptOnly,
			initialDocsDir,
			initialBatchInitialSourceGlob,
			initialSourceExcludeGlobs,
			onSave: async (settings: {
				promptOnly?: boolean;
				docsDir?: string;
				batchInitialSourceGlob?: string;
				sourceExcludeGlobs?: string[];
			}) => {

				let updatedYaml: string;

				try {
					const existingYaml = await fs.readFile(aiDevYamlPath, 'utf8');
					updatedYaml = existingYaml;
				} catch (error) {
					if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
						updatedYaml = '';
					} else {
						const message = error instanceof Error ? error.message : String(error);
						throw new Error(`Failed to read .ai-dev.yaml: ${message}`);
					}
				}

				if (typeof settings.promptOnly === 'boolean') {
					const selectedMode: AiProviderMode = settings.promptOnly ? 'prompt-only' : 'direct-experimental';
					updatedYaml = updateAiProviderModeInYaml(updatedYaml, selectedMode);
				}

				if (typeof settings.docsDir === 'string') {
					const docsDirValue = settings.docsDir.trim() || 'ai-docs';
					updatedYaml = updateYamlSectionScalarValue(updatedYaml, 'documentation', 'docsDir', docsDirValue);
				}

				if (typeof settings.batchInitialSourceGlob === 'string') {
					const batchInitialSourceGlobValue = settings.batchInitialSourceGlob.trim() || '**/*';
					updatedYaml = updateYamlSectionScalarValue(updatedYaml, 'documentation', 'batchInitialSourceGlob', batchInitialSourceGlobValue);
				}

				if (Array.isArray(settings.sourceExcludeGlobs)) {
					const normalizedGlobs = settings.sourceExcludeGlobs
						.map((glob) => glob.trim())
						.filter((glob) => glob.length > 0);
					updatedYaml = updateYamlSectionListValue(updatedYaml, 'source', 'exclude', normalizedGlobs);
				}

				try {
					await fs.writeFile(aiDevYamlPath, updatedYaml, 'utf8');
				} catch (error) {
					const message = error instanceof Error ? error.message : String(error);
					throw new Error(`Failed to write .ai-dev.yaml: ${message}`);
				}
			},
		});
	};

	const settingsCommand = vscode.commands.registerCommand(
		SETTINGS_COMMAND,
		openSettingsCommand
	);


	context.subscriptions.push(
		launchAssistantCommand,
		settingsCommand
	);
}

export function deactivate() {}
