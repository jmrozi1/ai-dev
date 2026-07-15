import * as vscode from 'vscode';
import * as fs from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import * as path from 'node:path';
import { registerAiDevActionsView } from './actionsView';
import {
	AiDevConfig,
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
	selectReviewFiles,
} from './projectReview';
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
const ARCHITECTURE_SUMMARY_FILE_NAME = 'architecture-summary.md';
const MAX_DIRECT_INDEX_UNIT_DOCS = 20;
const MAX_DIRECT_CHANGED_FILE_CONTENTS = 12;
const MAX_DIRECT_FILE_CHARS = 12000;
const MAX_DIRECT_DIFF_SAMPLE_FILES = 12;
const MAX_DIRECT_DIFF_CHARS = 8000;
const MAX_DIRECT_EXPECTED_DOC_CONTEXT_FILES = 12;
const MAX_DIRECT_SCOPED_INDEX_CONTEXT_FILES = 20;
const MAX_ROUTED_DOC_INDEX_FILES = 12;
const MAX_ROUTED_DOC_FILES = 24;
const MAX_ROUTED_DOC_FILE_CHARS = 6000;
const MAX_ROUTED_DOC_TOTAL_CHARS = 90000;
const MAX_FALLBACK_DISCOVERED_SUMMARIES = 6;
let allowDocsDirWritesForSession = false;

type AiProviderMode = 'prompt-only' | 'direct-experimental';

function isSupportedExecutionMode(value: string): value is AiProviderMode {
	return value === 'prompt-only' || value === 'direct-experimental';
}

function getExecutionModeFromConfig(config: AiDevConfig):
	| { mode: AiProviderMode }
	| { errorMessage: string } {
	const trimmedMode = config.aiProviderMode?.trim();
	if (!trimmedMode) {
		return { mode: 'direct-experimental' };
	}

	if (!isSupportedExecutionMode(trimmedMode)) {
		return {
			errorMessage: 'Unsupported aiProvider.mode in .ai-dev.yaml. Supported values: prompt-only, direct-experimental.',
		};
	}

	return { mode: trimmedMode };
}

function getRootSummaryFilePath(config: AiDevConfig): string {
	const configuredDocsDir = config.docsDir?.trim() || 'ai-docs';
	const normalizedDocsDir = normalizePathForMarkdown(configuredDocsDir).replace(/\/+$/, '');
	return path.posix.join(normalizedDocsDir, ARCHITECTURE_SUMMARY_FILE_NAME);
}

function getLegacyRootSummaryFilePath(config: AiDevConfig): string {
	const configuredDocsDir = config.docsDir?.trim() || 'ai-docs';
	const normalizedDocsDir = normalizePathForMarkdown(configuredDocsDir).replace(/\/+$/, '');
	return path.posix.join(normalizedDocsDir, 'summary.md');
}

function getArchitectureSummaryPath(config: AiDevConfig): string {
	const configuredDocsDir = getConfiguredDocsDir(config).replace(/\/+$/, '');
	return path.posix.join(configuredDocsDir, ARCHITECTURE_SUMMARY_FILE_NAME);
}

function getAiDevYamlPromptSection(config: AiDevConfig): { label: string; contents: string } {
	if (config.raw.trim().length === 0) {
		return {
			label: '.ai-dev.yaml: not present; using generic defaults',
			contents: '# .ai-dev.yaml not present; using generic defaults',
		};
	}

	return {
		label: '.ai-dev.yaml',
		contents: config.raw,
	};
}

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

function formatRelativePath(workspaceRoot: string, absolutePath: string): string {
	return normalizePathForMarkdown(path.relative(workspaceRoot, absolutePath));
}

function truncateText(content: string, maxChars: number): string {
	if (content.length <= maxChars) {
		return content;
	}

	return `${content.slice(0, maxChars)}\n...[truncated]`;
}

async function listMarkdownFilesRecursively(rootDirectory: string, maxFiles: number): Promise<string[]> {
	const results: string[] = [];

	const visit = async (directory: string): Promise<void> => {
		if (results.length >= maxFiles) {
			return;
		}

		let entries: Dirent[];
		try {
			entries = await fs.readdir(directory, { withFileTypes: true, encoding: 'utf8' });
		} catch {
			return;
		}

		for (const entry of entries) {
			if (results.length >= maxFiles) {
				break;
			}

			const entryPath = path.join(directory, entry.name);
			if (entry.isDirectory()) {
				await visit(entryPath);
				continue;
			}

			if (entry.isFile() && entry.name.toLowerCase().endsWith('.md')) {
				results.push(entryPath);
			}
		}
	};

	await visit(rootDirectory);
	return results;
}

interface DeterministicDocumentationFinding {
	title: string;
	details: string[];
	recommendation?: string;
}

function toDeterministicFindingsMarkdown(findings: DeterministicDocumentationFinding[]): string {
	if (findings.length === 0) {
		return [
			'## Deterministic Documentation Mapping Findings',
			'',
			'- none',
		].join('\n');
	}

	const lines: string[] = [
		'## Deterministic Documentation Mapping Findings',
		'',
	];

	for (const finding of findings) {
		lines.push(`### ${finding.title}`);
		lines.push('');
		for (const detail of finding.details) {
			lines.push(`- ${detail}`);
		}
		if (finding.recommendation) {
			lines.push(`- Recommendation: ${finding.recommendation}`);
		}
		lines.push('');
	}

	return lines.join('\n').trimEnd();
}

async function collectDeterministicDocumentationFindings(params: {
	workspaceRoot: string;
	aiDevConfig: AiDevConfig;
	changedFilePaths: string[];
	renameRecords: GitRenameRecord[];
}): Promise<DeterministicDocumentationFinding[]> {
	const { workspaceRoot, aiDevConfig, changedFilePaths, renameRecords } = params;
	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const normalizedDocsDir = normalizePathForMarkdown(configuredDocsDir).replace(/\/+$/, '');
	const docsDirPrefix = `${normalizedDocsDir}/`;
	const { excludeGlobs } = getBatchSourceGlobs(aiDevConfig);
	const sourceCandidates = await discoverBatchUnitDocCandidates(workspaceRoot, aiDevConfig);
	const normalizedSourceCandidates = sourceCandidates
		.map((sourceAbsolutePath) => normalizePathForMarkdown(path.relative(workspaceRoot, sourceAbsolutePath)))
		.sort((left, right) => left.localeCompare(right));
	const sourceByExpectedSummaryPath = new Map<string, string[]>();
	for (const sourcePath of normalizedSourceCandidates) {
		const expectedSummaryPath = getExpectedDirectorySummaryPath({
			workspaceRoot,
			sourceFilePath: path.resolve(workspaceRoot, sourcePath),
			docsDir: configuredDocsDir,
		});
		const matches = sourceByExpectedSummaryPath.get(expectedSummaryPath) ?? [];
		matches.push(sourcePath);
		sourceByExpectedSummaryPath.set(expectedSummaryPath, matches);
	}

	const findings: DeterministicDocumentationFinding[] = [];
	const normalizedChangedPaths = changedFilePaths
		.map((filePath) => normalizePathForMarkdown(filePath))
		.filter((filePath) => filePath.length > 0);

	for (const changedPath of normalizedChangedPaths) {
		if (isConfiguredSourceCandidatePath(changedPath, configuredDocsDir, excludeGlobs)) {
			const sourceAbsolutePath = path.resolve(workspaceRoot, changedPath);
			const expectedSummaryPath = getExpectedDirectorySummaryPath({
				workspaceRoot,
				sourceFilePath: sourceAbsolutePath,
				docsDir: configuredDocsDir,
			});
			const expectedSummaryAbsolutePath = path.resolve(workspaceRoot, expectedSummaryPath);
			const expectedSummaryContents = await readOptionalTextFile(expectedSummaryAbsolutePath);
			if (expectedSummaryContents === undefined || expectedSummaryContents.trim().length === 0) {
				findings.push({
					title: 'Missing expected summary',
					details: [
						`Source path: ${changedPath}`,
						`Expected summary path: ${expectedSummaryPath}`,
					],
				});
			}
		}

		if (!changedPath.startsWith(docsDirPrefix)) {
			continue;
		}

		const changedDocAbsolutePath = path.resolve(workspaceRoot, changedPath);
		if (!(await fileExists(changedDocAbsolutePath))) {
			continue;
		}

		const changedDocBaseName = path.posix.basename(changedPath).toLowerCase();
		if (
			changedDocBaseName === 'summary.md'
			|| changedDocBaseName === ARCHITECTURE_SUMMARY_FILE_NAME
			|| changedDocBaseName === 'dependency-map.md'
		) {
			continue;
		}

		if (!sourceByExpectedSummaryPath.has(changedPath)) {
			findings.push({
				title: 'Documentation file has no matching source',
				details: [
					`Documentation path: ${changedPath}`,
				],
			});
		}
	}

	for (const renameRecord of renameRecords) {
		const oldSourcePath = normalizePathForMarkdown(renameRecord.oldPath);
		const newSourcePath = normalizePathForMarkdown(renameRecord.newPath);
		if (!isConfiguredSourceCandidatePath(newSourcePath, configuredDocsDir, excludeGlobs)) {
			continue;
		}

		const oldExpectedSummaryPath = getExpectedDirectorySummaryPath({
			workspaceRoot,
			sourceFilePath: path.resolve(workspaceRoot, oldSourcePath),
			docsDir: configuredDocsDir,
		});
		const newExpectedSummaryPath = getExpectedDirectorySummaryPath({
			workspaceRoot,
			sourceFilePath: path.resolve(workspaceRoot, newSourcePath),
			docsDir: configuredDocsDir,
		});

		const oldExpectedSummaryAbsolutePath = path.resolve(workspaceRoot, oldExpectedSummaryPath);
		const newExpectedSummaryAbsolutePath = path.resolve(workspaceRoot, newExpectedSummaryPath);
		const [oldExpectedDocExists, newExpectedDocExists] = await Promise.all([
			fileExists(oldExpectedSummaryAbsolutePath),
			fileExists(newExpectedSummaryAbsolutePath),
		]);

		if (oldExpectedDocExists && !newExpectedDocExists) {
			findings.push({
				title: 'Source moved but documentation was not moved',
				details: [
					`Old source path: ${oldSourcePath}`,
					`New source path: ${newSourcePath}`,
					`Old summary path: ${oldExpectedSummaryPath}`,
					`New expected summary path: ${newExpectedSummaryPath}`,
				],
				recommendation: 'Update or regenerate the destination summary file so it includes the moved source file entry.',
			});
		}
	}

	return findings;
}

interface BatchUnitDocGenerationCandidate {
	sourceAbsolutePath: string;
	sourcePath: string;
	expectedDocPath: string;
	expectedDocAbsolutePath: string;
	docStatus: BatchUnitDocStatus;
}

interface BatchUnitDocPlanAction {
	actionType: BatchUnitDocActionType;
	actionLabel: BatchUnitDocPreviewItem['actionLabel'];
	apply: boolean;
	sourcePath?: string;
	docPath: string;
	targetDocPath?: string;
	notes: string;
}

interface BatchUnitDocSelectionResult {
	normalizedSourceGlob: string;
	counts: BatchUnitDocPreviewResult['counts'];
	items: BatchUnitDocPreviewItem[];
	plannedActions: BatchUnitDocPlanAction[];
}

interface ArchitectureSummaryDirectoryCandidate {
	sourceDirectory: string;
	summaryPath: string;
	summaryAbsolutePath: string;
	status: ArchitectureSummaryStatus;
	notes: string;
	applyByDefault: boolean;
}

function getBatchActionLabel(actionType: BatchUnitDocActionType): BatchUnitDocPreviewItem['actionLabel'] {
	switch (actionType) {
		case 'generate-doc':
			return 'Update summary';
		case 'move-doc':
			return 'Move orphan doc';
		case 'delete-doc':
			return 'Delete orphan doc';
		default:
			return 'Update summary';
	}
}

function getBatchDefaultApply(actionType: BatchUnitDocActionType): boolean {
	return actionType === 'generate-doc';
}

function normalizeSelectedSourceDirectory(selectedSourceDirectory: string | undefined): string | undefined {
	if (!selectedSourceDirectory) {
		return undefined;
	}

	const normalized = normalizePathForMarkdown(selectedSourceDirectory.trim())
		.replace(/^\.\//, '')
		.replace(/\/+$/, '');
	if (!normalized || normalized === '.') {
		return '.';
	}

	return normalized;
}

function getSourceDirectoryPath(sourcePath: string): string {
	const directoryPath = normalizePathForMarkdown(path.posix.dirname(sourcePath));
	return directoryPath === '' ? '.' : directoryPath;
}

function getDocStatusForExistingContent(existingContent: string | undefined): BatchUnitDocStatus {
	if (existingContent === undefined) {
		return 'missing';
	}

	if (existingContent.trim().length === 0) {
		return 'empty';
	}

	return 'exists';
}

function getExpectedSummaryPathForSourceDirectory(docsDir: string, sourceDirectory: string): string {
	const normalizedSourceDirectory = normalizeSelectedSourceDirectory(sourceDirectory) ?? '.';
	if (normalizedSourceDirectory === '.') {
		return path.posix.join(docsDir, 'summary.md');
	}

	return path.posix.join(docsDir, normalizedSourceDirectory, 'summary.md');
}

function getArchitectureDefaultApply(status: ArchitectureSummaryStatus): boolean {
	return status !== 'missing';
}

function formatSourceDirectoryForDisplay(sourceDirectory: string): string {
	if (sourceDirectory === '.') {
		return '.';
	}

	return sourceDirectory.endsWith('/') ? sourceDirectory : `${sourceDirectory}/`;
}

function countArchitectureStatuses(items: ArchitectureSummaryPreviewItem[]): {
	existingCount: number;
	missingCount: number;
	emptyCount: number;
} {
	let existingCount = 0;
	let missingCount = 0;
	let emptyCount = 0;

	for (const item of items) {
		if (item.status === 'exists') {
			existingCount += 1;
			continue;
		}

		if (item.status === 'missing') {
			missingCount += 1;
			continue;
		}

		emptyCount += 1;
	}

	return { existingCount, missingCount, emptyCount };
}

async function discoverArchitectureSummaryPreview(params: {
	workspaceRoot: string;
	aiDevConfig: AiDevConfig;
}): Promise<ArchitectureSummaryPreviewResult> {
	const { workspaceRoot, aiDevConfig } = params;
	const sourceCandidates = await discoverBatchUnitDocCandidates(workspaceRoot, aiDevConfig);
	const docsDir = getConfiguredDocsDir(aiDevConfig);
	const directorySet = new Set<string>();

	for (const sourceAbsolutePath of sourceCandidates) {
		const sourcePath = normalizePathForMarkdown(path.relative(workspaceRoot, sourceAbsolutePath));
		directorySet.add(getSourceDirectoryPath(sourcePath));
	}

	const sortedDirectories = [...directorySet].sort((left, right) => {
		if (left === '.') {
			return -1;
		}

		if (right === '.') {
			return 1;
		}

		return left.localeCompare(right);
	});

	const candidates: ArchitectureSummaryDirectoryCandidate[] = await Promise.all(sortedDirectories.map(async (sourceDirectory) => {
		const summaryPath = getExpectedSummaryPathForSourceDirectory(docsDir, sourceDirectory);
		const summaryAbsolutePath = path.resolve(workspaceRoot, summaryPath);
		const summaryContents = await readOptionalTextFile(summaryAbsolutePath);
		const status = getDocStatusForExistingContent(summaryContents);

		const notes = status === 'exists'
			? 'Included'
			: status === 'empty'
				? 'Included (known gap: empty summary)'
				: 'Known gap: missing summary';

		return {
			sourceDirectory,
			summaryPath,
			summaryAbsolutePath,
			status,
			notes,
			applyByDefault: getArchitectureDefaultApply(status),
		};
	}));

	const items: ArchitectureSummaryPreviewItem[] = candidates.map((candidate) => ({
		apply: candidate.applyByDefault,
		sourceDirectory: formatSourceDirectoryForDisplay(candidate.sourceDirectory),
		summaryPath: candidate.summaryPath,
		status: candidate.status,
		notes: candidate.notes,
	}));

	const statusCounts = countArchitectureStatuses(items);

	return {
		counts: {
			totalDirectories: items.length,
			existingSummaries: statusCounts.existingCount,
			missingSummaries: statusCounts.missingCount,
			emptySummaries: statusCounts.emptyCount,
		},
		items,
	};
}

function summaryContainsSourceEntry(summaryContents: string | undefined, sourcePath: string): boolean {
	if (!summaryContents || summaryContents.trim().length === 0) {
		return false;
	}

	const normalizedSourcePath = normalizePathForMarkdown(sourcePath);
	const escapedSourcePath = normalizedSourcePath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const entryPattern = new RegExp(`(^|\\n)\\s*-\\s*\\\`${escapedSourcePath}\\\`\\s*[—-]`, 'm');
	return entryPattern.test(summaryContents);
}

async function computeBatchUnitDocSelection(params: {
	workspaceRoot: string;
	aiDevConfig: AiDevConfig;
	formState: BatchUnitDocsFormState;
}): Promise<BatchUnitDocSelectionResult> {
	const { workspaceRoot, aiDevConfig, formState } = params;
	const configuredCandidates = await discoverBatchUnitDocCandidates(workspaceRoot, aiDevConfig);
	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const normalizedSourceGlob = normalizeBatchSourceGlob(formState.sourceGlob, aiDevConfig);
	const normalizedSelectedSourceDirectory = normalizeSelectedSourceDirectory(formState.selectedSourceDirectory);
	const docsDirAbsolutePath = path.resolve(workspaceRoot, configuredDocsDir);
	const scopedCandidates = formState.selectionMode === 'folder' && normalizedSelectedSourceDirectory
		? configuredCandidates.filter((sourceAbsolutePath) => {
			const sourcePath = normalizePathForMarkdown(path.relative(workspaceRoot, sourceAbsolutePath));
			return sourcePath === normalizedSelectedSourceDirectory || sourcePath.startsWith(`${normalizedSelectedSourceDirectory}/`);
		})
		: configuredCandidates;

	const configuredCandidatesWithStatus = await Promise.all(scopedCandidates.map(async (sourceAbsolutePath) => {
		const sourcePath = normalizePathForMarkdown(path.relative(workspaceRoot, sourceAbsolutePath));
		const expectedSummaryPath = getExpectedDirectorySummaryPath({
			workspaceRoot,
			sourceFilePath: sourceAbsolutePath,
			docsDir: configuredDocsDir,
		});
		const expectedSummaryAbsolutePath = path.resolve(workspaceRoot, expectedSummaryPath);
		const existingContent = await readOptionalTextFile(expectedSummaryAbsolutePath);
		const docStatus = getDocStatusForExistingContent(existingContent);
		const hasSummaryEntry = summaryContainsSourceEntry(existingContent, sourcePath);

		return {
			sourceAbsolutePath,
			sourcePath,
			expectedDocPath: expectedSummaryPath,
			expectedDocAbsolutePath: expectedSummaryAbsolutePath,
			docStatus,
			hasSummaryEntry,
		};
	}));

	const candidateWithStatus = configuredCandidatesWithStatus.filter((candidate) => matchesAnyGlob(candidate.sourcePath, [normalizedSourceGlob]));

	const afterMissingDocFilter = formState.missingDocsOnly
		? candidateWithStatus.filter((candidate) => candidate.docStatus !== 'exists' || !candidate.hasSummaryEntry)
		: candidateWithStatus;

	afterMissingDocFilter.sort((left, right) => left.sourcePath.localeCompare(right.sourcePath));

	const filesThisPassLimit = Math.min(
		MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
		Math.max(1, Number.isFinite(formState.maxFiles) ? Math.floor(formState.maxFiles) : DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS)
	);
	const limitedCandidates = afterMissingDocFilter.slice(0, filesThisPassLimit);

	const plannedActions: BatchUnitDocPlanAction[] = limitedCandidates.map((candidate) => ({
		actionType: 'generate-doc',
		actionLabel: getBatchActionLabel('generate-doc'),
		apply: getBatchDefaultApply('generate-doc'),
		sourcePath: candidate.sourcePath,
		docPath: candidate.expectedDocPath,
		notes: candidate.docStatus === 'exists' && candidate.hasSummaryEntry
			? 'Existing summary entry will be updated.'
			: 'Add missing summary entry.',
	}));

	if (formState.resolveOrphanedDocs) {
		const docsDirFiles = await listMarkdownFilesRecursively(docsDirAbsolutePath, Number.POSITIVE_INFINITY);
		const sourceByExpectedDocPath = new Map<string, BatchUnitDocGenerationCandidate>();
		for (const candidate of configuredCandidatesWithStatus) {
			sourceByExpectedDocPath.set(candidate.expectedDocPath, candidate);
		}

		for (const orphanAbsolutePath of docsDirFiles) {
			const orphanDocPath = normalizePathForMarkdown(path.relative(workspaceRoot, orphanAbsolutePath));
			if (path.posix.basename(orphanDocPath).toLowerCase() === 'summary.md') {
				continue;
			}

			if (sourceByExpectedDocPath.has(orphanDocPath)) {
				continue;
			}

			plannedActions.push({
				actionType: 'delete-doc',
				actionLabel: getBatchActionLabel('delete-doc'),
				apply: getBatchDefaultApply('delete-doc'),
				docPath: orphanDocPath,
				notes: 'Legacy per-file doc with no summary-path mapping. Destructive cleanup is unchecked by default.',
			});
		}
	}

	plannedActions.sort((left, right) => {
		const leftActionRank = left.actionType === 'generate-doc' ? 1 : left.actionType === 'move-doc' ? 2 : 3;
		const rightActionRank = right.actionType === 'generate-doc' ? 1 : right.actionType === 'move-doc' ? 2 : 3;
		if (leftActionRank !== rightActionRank) {
			return leftActionRank - rightActionRank;
		}

		return left.docPath.localeCompare(right.docPath);
	});

	return {
		normalizedSourceGlob,
		counts: {
			totalConfiguredSourceCandidates: scopedCandidates.length,
			afterGlobFilter: candidateWithStatus.length,
			afterMissingDocFilter: afterMissingDocFilter.length,
			previewCount: plannedActions.length,
		},
		items: plannedActions.map((action) => ({
			apply: action.apply,
			sourcePath: action.sourcePath,
			docPath: action.docPath,
			actionType: action.actionType,
			actionLabel: action.actionLabel,
			notes: action.notes,
			targetDocPath: action.targetDocPath,
		})),
		plannedActions,
	};
}

async function fileExists(filePath: string): Promise<boolean> {
	try {
		await fs.access(filePath);
		return true;
	} catch {
		return false;
	}
}

async function readOptionalTextFile(filePath: string): Promise<string | undefined> {
	try {
		return await fs.readFile(filePath, 'utf8');
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
			return undefined;
		}

		throw error;
	}
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

interface ChangedSourceDocumentationContext {
	sourcePath: string;
	expectedDocPath: string;
	expectedDocContents?: string;
}

function getScopedSummaryPathCandidatesForExpectedDoc(params: {
	expectedDocPath: string;
	docsDir: string;
	rootSummaryFile: string;
}): string[] {
	const normalizedDocsDir = normalizePathForMarkdown(params.docsDir).replace(/\/+$/, '');
	const normalizedExpectedDocPath = normalizePathForMarkdown(params.expectedDocPath);
	const candidates = new Set<string>([normalizePathForMarkdown(params.rootSummaryFile)]);

	let currentDirectory = path.posix.dirname(normalizedExpectedDocPath);
	while (currentDirectory.length > 0 && currentDirectory !== '.' && (currentDirectory === normalizedDocsDir || currentDirectory.startsWith(`${normalizedDocsDir}/`))) {
		candidates.add(normalizePathForMarkdown(path.posix.join(currentDirectory, 'summary.md')));
		if (currentDirectory === normalizedDocsDir) {
			break;
		}

		const parentDirectory = path.posix.dirname(currentDirectory);
		if (parentDirectory === currentDirectory) {
			break;
		}

		currentDirectory = parentDirectory;
	}

	return [...candidates];
}

interface RoutedDocumentationFile {
	path: string;
	kind: 'summary' | 'dependency-map' | 'routing-artifact';
	contents: string;
}

interface RoutedDocumentationContext {
	routedFiles: RoutedDocumentationFile[];
	missingPaths: string[];
}

interface DiscoveredSummaryFile {
	absolutePath: string;
	relativePath: string;
	contents: string;
	score: number;
}

interface ParsedMarkdownLink {
	label: string;
	target: string;
}

function parseMarkdownLinks(markdown: string): ParsedMarkdownLink[] {
	const links: ParsedMarkdownLink[] = [];
	const regex = /\[([^\]]*)\]\(([^)]+)\)/g;
	let match: RegExpExecArray | null;
	while ((match = regex.exec(markdown)) !== null) {
		const label = match[1]?.trim();
		const target = match[2]?.trim();
		if (!target) {
			continue;
		}

		links.push({
			label: label ?? '',
			target,
		});
	}

	return links;
}

function tokenizeQuestionForRouting(question: string): string[] {
	const uniqueTokens = new Set(
		question
			.toLowerCase()
			.split(/[^a-z0-9]+/)
			.filter((token) => token.length >= 3)
	);
	return [...uniqueTokens];
}

function scoreLinkRelevance(linkLabel: string, targetPath: string, questionTokens: string[]): number {
	if (questionTokens.length === 0) {
		return 0;
	}

	const haystack = `${linkLabel} ${targetPath}`.toLowerCase();
	let score = 0;
	for (const token of questionTokens) {
		if (haystack.includes(token)) {
			score += 1;
		}
	}

	return score;
}

function scoreDiscoveredSummaryRelevance(summaryPath: string, summaryContents: string, questionTokens: string[]): number {
	if (questionTokens.length === 0) {
		return 0;
	}

	const normalizedPath = normalizePathForMarkdown(summaryPath).toLowerCase();
	const lowerContents = summaryContents.toLowerCase();
	let score = 0;

	for (const token of questionTokens) {
		if (normalizedPath.includes(token)) {
			score += 3;
		}

		if (lowerContents.includes(token)) {
			score += 1;
		}
	}

	return score;
}

async function discoverSummaryFilesRecursively(docsDirAbsolutePath: string): Promise<string[]> {
	const discoveredPaths: string[] = [];
	const pendingDirectories: string[] = [docsDirAbsolutePath];

	while (pendingDirectories.length > 0) {
		const currentDirectory = pendingDirectories.pop();
		if (!currentDirectory) {
			continue;
		}

		let entries: Dirent[];
		try {
			entries = await fs.readdir(currentDirectory, { withFileTypes: true, encoding: 'utf8' });
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
				continue;
			}

			throw error;
		}

		for (const entry of entries) {
			const entryPath = path.join(currentDirectory, entry.name);
			if (entry.isDirectory()) {
				pendingDirectories.push(entryPath);
				continue;
			}

			if (entry.isFile() && entry.name.toLowerCase() === 'summary.md') {
				discoveredPaths.push(entryPath);
			}
		}
	}

	discoveredPaths.sort((left, right) => left.localeCompare(right));
	return discoveredPaths;
}

async function selectFallbackDiscoveredSummaries(params: {
	workspaceRoot: string;
	discoveredSummaryPaths: string[];
	excludeAbsolutePaths: Set<string>;
	userQuestion: string;
	maxSummaries: number;
}): Promise<DiscoveredSummaryFile[]> {
	const questionTokens = tokenizeQuestionForRouting(params.userQuestion);
	const candidates: DiscoveredSummaryFile[] = [];

	for (const discoveredAbsolutePath of params.discoveredSummaryPaths) {
		if (params.excludeAbsolutePaths.has(discoveredAbsolutePath)) {
			continue;
		}

		const discoveredContents = await readOptionalTextFile(discoveredAbsolutePath);
		if (!discoveredContents || discoveredContents.trim().length === 0) {
			continue;
		}

		const relativePath = normalizePathForMarkdown(path.relative(params.workspaceRoot, discoveredAbsolutePath));
		candidates.push({
			absolutePath: discoveredAbsolutePath,
			relativePath,
			contents: discoveredContents,
			score: scoreDiscoveredSummaryRelevance(relativePath, discoveredContents, questionTokens),
		});
	}

	candidates.sort((left, right) => {
		if (left.score !== right.score) {
			return right.score - left.score;
		}

		return left.relativePath.localeCompare(right.relativePath);
	});

	const relevant = candidates.filter((item) => item.score > 0);
	if (relevant.length > 0) {
		return relevant.slice(0, params.maxSummaries);
	}

	return candidates.slice(0, Math.min(2, params.maxSummaries));
}

function resolveLinkedMarkdownPath(params: {
	workspaceRoot: string;
	docsDirAbsolutePath: string;
	baseFilePath: string;
	rawTarget: string;
}): string | undefined {
	const { workspaceRoot, docsDirAbsolutePath, baseFilePath, rawTarget } = params;
	const trimmed = rawTarget.trim();
	if (trimmed.length === 0) {
		return undefined;
	}

	if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) {
		return undefined;
	}

	if (trimmed.startsWith('#')) {
		return undefined;
	}

	const targetWithoutFragment = trimmed.split('#', 1)[0] ?? '';
	const targetWithoutQuery = targetWithoutFragment.split('?', 1)[0] ?? '';
	if (!targetWithoutQuery.toLowerCase().endsWith('.md')) {
		return undefined;
	}

	const absolutePath = path.resolve(path.dirname(baseFilePath), targetWithoutQuery);
	if (!isPathInsideDirectory(absolutePath, workspaceRoot)) {
		return undefined;
	}

	if (!isPathInsideDirectory(absolutePath, docsDirAbsolutePath)) {
		return undefined;
	}

	return absolutePath;
}

async function collectRoutedDocumentationContextForAnswer(params: {
	workspaceRoot: string;
	docsDirAbsolutePath: string;
	rootSummaryPath: string;
	userQuestion: string;
}): Promise<RoutedDocumentationContext> {
	const rootSummaryContents = await readOptionalTextFile(params.rootSummaryPath);
	if (!rootSummaryContents) {
		return {
			routedFiles: [],
			missingPaths: [],
		};
	}

	const questionTokens = tokenizeQuestionForRouting(params.userQuestion);
	const routedFiles: RoutedDocumentationFile[] = [];
	const missingPaths: string[] = [];
	const seenMissingPaths = new Set<string>();
	const visitedSummaryPaths = new Set<string>();
	const visitedDocPaths = new Set<string>();
	const pendingSummaries: Array<{ absolutePath: string; contents: string }> = [];
	let totalChars = 0;

	const pushRoutedFile = (kind: 'summary' | 'dependency-map' | 'routing-artifact', absolutePath: string, contents: string): boolean => {
		const remainingChars = MAX_ROUTED_DOC_TOTAL_CHARS - totalChars;
		if (remainingChars <= 0) {
			return false;
		}

		const clippedContents = contents.slice(0, Math.min(MAX_ROUTED_DOC_FILE_CHARS, remainingChars));
		totalChars += clippedContents.length;
		routedFiles.push({
			path: normalizePathForMarkdown(path.relative(params.workspaceRoot, absolutePath)),
			kind,
			contents: clippedContents,
		});
		return true;
	};

	visitedSummaryPaths.add(params.rootSummaryPath);
	pendingSummaries.push({ absolutePath: params.rootSummaryPath, contents: rootSummaryContents });

	while (pendingSummaries.length > 0) {
		const currentSummary = pendingSummaries.shift();
		if (!currentSummary) {
			break;
		}

		const parsedLinks = parseMarkdownLinks(currentSummary.contents)
			.map((link) => ({
				...link,
				resolvedPath: resolveLinkedMarkdownPath({
					workspaceRoot: params.workspaceRoot,
					docsDirAbsolutePath: params.docsDirAbsolutePath,
					baseFilePath: currentSummary.absolutePath,
					rawTarget: link.target,
				}),
			}))
			.filter((link) => typeof link.resolvedPath === 'string')
			.map((link) => ({
				label: link.label,
				resolvedPath: link.resolvedPath as string,
				score: scoreLinkRelevance(link.label, link.resolvedPath as string, questionTokens),
			}));

		parsedLinks.sort((left, right) => {
			if (left.score !== right.score) {
				return right.score - left.score;
			}

			return left.resolvedPath.localeCompare(right.resolvedPath);
		});

		for (const link of parsedLinks) {
			const normalizedPath = normalizePathForMarkdown(link.resolvedPath);
			const basename = path.basename(normalizedPath).toLowerCase();
			const routedKind: 'summary' | 'dependency-map' | 'routing-artifact' = basename === 'summary.md'
				? 'summary'
				: basename === 'dependency-map.md'
					? 'dependency-map'
					: 'routing-artifact';
			const isSummary = routedKind === 'summary';
			if (isSummary) {
				if (visitedSummaryPaths.has(link.resolvedPath)) {
					continue;
				}

				if (visitedSummaryPaths.size >= MAX_ROUTED_DOC_INDEX_FILES) {
					continue;
				}

				visitedSummaryPaths.add(link.resolvedPath);
				const linkedContents = await readOptionalTextFile(link.resolvedPath);
				if (!linkedContents) {
					const missingPath = normalizePathForMarkdown(path.relative(params.workspaceRoot, link.resolvedPath));
					if (!seenMissingPaths.has(missingPath)) {
						seenMissingPaths.add(missingPath);
						missingPaths.push(missingPath);
					}
					continue;
				}

				if (pushRoutedFile('summary', link.resolvedPath, linkedContents)) {
					pendingSummaries.push({ absolutePath: link.resolvedPath, contents: linkedContents });
				}
				continue;
			}

			if (visitedDocPaths.has(link.resolvedPath)) {
				continue;
			}

			if (visitedDocPaths.size >= MAX_ROUTED_DOC_FILES) {
				continue;
			}

			visitedDocPaths.add(link.resolvedPath);
			const linkedContents = await readOptionalTextFile(link.resolvedPath);
			if (!linkedContents) {
				const missingPath = normalizePathForMarkdown(path.relative(params.workspaceRoot, link.resolvedPath));
				if (!seenMissingPaths.has(missingPath)) {
					seenMissingPaths.add(missingPath);
					missingPaths.push(missingPath);
				}
				continue;
			}

			if (!pushRoutedFile(routedKind, link.resolvedPath, linkedContents)) {
				break;
			}
		}
	}

	return {
		routedFiles,
		missingPaths,
	};
}

function stripSingleOuterCodeFence(text: string): { text: string; stripped: boolean } {
	const trimmed = text.trim();
	const fencedBlockMatch = trimmed.match(/^```[^\r\n]*\r?\n([\s\S]*?)\r?\n```$/);
	if (!fencedBlockMatch) {
		return { text, stripped: false };
	}

	return { text: fencedBlockMatch[1], stripped: true };
}

async function writeTextFileAtomicish(targetAbsolutePath: string, content: string): Promise<void> {
	const tempFilePath = `${targetAbsolutePath}.tmp-${Date.now()}`;
	let tempFileCreated = false;

	try {
		await fs.writeFile(tempFilePath, content, 'utf8');
		tempFileCreated = true;
		await fs.rename(tempFilePath, targetAbsolutePath);
		tempFileCreated = false;
	} catch (error) {
		if (tempFileCreated) {
			try {
				await fs.unlink(tempFilePath);
			} catch {
				// Ignore cleanup errors so the original write failure can be surfaced.
			}
		}

		throw error;
	}
}

async function buildGenerateUnitDocDirectPromptBundle(params: {
	workspaceRoot: string;
	activeFileUri: vscode.Uri;
	aiDevConfig: AiDevConfig;
}): Promise<{
	directPromptMarkdown: string;
	selectedSourcePath: string;
	expectedSummaryPath: string;
}> {
	const { workspaceRoot, activeFileUri, aiDevConfig } = params;
	const aiDevYamlSection = getAiDevYamlPromptSection(aiDevConfig);

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const selectedSourcePath = getSelectedSourcePath(workspaceRoot, activeFileUri.fsPath);
	const expectedSummaryPath = getExpectedDirectorySummaryPath({
		workspaceRoot,
		sourceFilePath: activeFileUri.fsPath,
		docsDir: configuredDocsDir,
	});
	const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePath);
	const workflowFilePath = path.join(aiDevCorePath, 'workflows/generate-docs/generate-unit-doc.md');
	const templateFilePath = path.join(aiDevCorePath, 'workflows/generate-docs/templates/unit-doc.md');
	const sourceFilePath = activeFileUri.fsPath;
	const expectedSummaryAbsolutePath = path.resolve(workspaceRoot, expectedSummaryPath);

	const [workflowFileContents, templateFileContents, sourceFileContents, expectedSummaryContents, aiDevYamlContents] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		fs.readFile(templateFilePath, 'utf8'),
		fs.readFile(sourceFilePath, 'utf8'),
		readOptionalTextFile(expectedSummaryAbsolutePath),
		Promise.resolve(aiDevYamlSection.contents),
	]);

	const directPromptMarkdown = [
		'AI Dev direct task: generate-unit-doc',
		'',
		`Workspace: ${normalizePathForMarkdown(workspaceRoot)}`,
		`Source file path: ${selectedSourcePath}`,
		`Target summary file path: ${expectedSummaryPath}`,
		'',
		'Workflow file:',
		formatRelativePath(workspaceRoot, workflowFilePath),
		'',
		'```markdown',
		workflowFileContents,
		'```',
		'',
		'Template file:',
		formatRelativePath(workspaceRoot, templateFilePath),
		'',
		'```markdown',
		templateFileContents,
		'```',
		'',
		'.ai-dev.yaml:',
		aiDevYamlSection.label,
		'',
		'```yaml',
		aiDevYamlContents,
		'```',
		'',
		'Source file contents:',
		selectedSourcePath,
		'',
		'```text',
		sourceFileContents,
		'```',
		'',
		expectedSummaryContents ? 'Existing target summary contents:' : 'Existing target summary contents: not found',
		expectedSummaryContents ? expectedSummaryPath : '',
		...(expectedSummaryContents
			? [
				'',
				'```markdown',
				expectedSummaryContents,
				'```',
			]
			: []),
		'',
		'Instructions:',
		'Generate the complete updated markdown for the target summary file.',
		'Update or add the selected source file entry inside that summary file.',
		'Do not generate a standalone per-source documentation file.',
		'Treat the source file as final authority and follow the workflow and template.',
		'Do not write files or describe changes; return only the final summary markdown contents.',
		'Return the complete updated contents of the target summary.md file.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.',
	].join('\n');

	return {
		directPromptMarkdown,
		selectedSourcePath,
		expectedSummaryPath,
	};
}


async function buildReviewDocumentationDirectPromptBundle(workspaceRoot: string, aiDevConfig: AiDevConfig): Promise<{
	directPromptMarkdown: string;
	changedFilePaths: string[];
	deterministicFindings: DeterministicDocumentationFinding[];
	gitDiffs: GitFileDiff[];
}> {
	const aiDevYamlSection = getAiDevYamlPromptSection(aiDevConfig);

	let changedFilePaths: string[];
	let renameRecords: GitRenameRecord[];
	try {
		changedFilePaths = await getGitChangedFiles(workspaceRoot);
		renameRecords = await getGitRenameRecords(workspaceRoot);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		throw new Error(`Failed to read git changed files: ${message}`);
	}

	if (changedFilePaths.length === 0) {
		throw new Error('No changed files found.');
	}

	const reviewableChangedFilePaths = changedFilePaths.filter(
		(filePath) =>
			!matchesAnyGlob(
				normalizePathForMarkdown(filePath),
				NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS
			)
	);

	if (reviewableChangedFilePaths.length === 0) {
		throw new Error(
			'No reviewable changed files found after excluding packaged binaries and non-source artifacts.'
		);
	}

	const ignoredArtifactChangeCount =
		changedFilePaths.length
		- reviewableChangedFilePaths.length;

	const deterministicFindings =
		await collectDeterministicDocumentationFindings({
			workspaceRoot,
			aiDevConfig,
			changedFilePaths: reviewableChangedFilePaths,
			renameRecords,
		});

	const deterministicFindingsMarkdown =
		toDeterministicFindingsMarkdown(
			deterministicFindings
		);

	const aiDevCorePath = resolveAiDevCorePath(
		workspaceRoot,
		aiDevConfig.aiDevCorePath
	);
	const workflowFilePath = path.join(
		aiDevCorePath,
		'workflows/review/review-documentation.md'
	);
	const findingTemplatePath = path.join(
		aiDevCorePath,
		'workflows/review/finding-template.md'
	);

	const [
		workflowFileContents,
		findingTemplateContents,
		aiDevYamlContents,
		changedFilesWithContent,
		gitDiffs,
	] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		fs.readFile(findingTemplatePath, 'utf8'),
		Promise.resolve(aiDevYamlSection.contents),
		existingChangedFilesWithContent(
			workspaceRoot,
			reviewableChangedFilePaths
		),
		getGitDiffForFiles(
			workspaceRoot,
			reviewableChangedFilePaths
		),
	]);

	const boundedChangedFilesWithContent = changedFilesWithContent
		.slice(0, MAX_DIRECT_CHANGED_FILE_CONTENTS)
		.map((file) => ({
			relativePath: file.relativePath,
			contents: truncateText(file.contents, MAX_DIRECT_FILE_CHARS),
		}));

	const normalizedChangedFilePaths =
		reviewableChangedFilePaths.map(
			(filePath) =>
				normalizePathForMarkdown(filePath)
		);
	const boundedGitDiffs = gitDiffs
		.slice(0, MAX_DIRECT_DIFF_SAMPLE_FILES)
		.map((item) => ({
			relativePath: normalizePathForMarkdown(item.relativePath),
			diff: truncateText(item.diff, MAX_DIRECT_DIFF_CHARS),
		}));

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const rootSummaryFile = getRootSummaryFilePath(aiDevConfig);
	const { excludeGlobs } = getBatchSourceGlobs(aiDevConfig);

	const changedSourcePaths = normalizedChangedFilePaths
		.filter((filePath) => isConfiguredSourceCandidatePath(filePath, configuredDocsDir, excludeGlobs))
		.slice(0, MAX_DIRECT_EXPECTED_DOC_CONTEXT_FILES);

	const expectedDocumentationContext: ChangedSourceDocumentationContext[] = await Promise.all(
		changedSourcePaths.map(async (sourcePath) => {
			const expectedSummaryPath = getExpectedDirectorySummaryPath({
				workspaceRoot,
				sourceFilePath: path.resolve(workspaceRoot, sourcePath),
				docsDir: configuredDocsDir,
			});
			const expectedSummaryContents = await readOptionalTextFile(path.resolve(workspaceRoot, expectedSummaryPath));
			return {
				sourcePath,
				expectedDocPath: expectedSummaryPath,
				expectedDocContents: expectedSummaryContents ? truncateText(expectedSummaryContents, MAX_DIRECT_FILE_CHARS) : undefined,
			};
		})
	);

	const scopedSummaryCandidates = new Set<string>();
	for (const entry of expectedDocumentationContext) {
		for (const scopedSummaryPath of getScopedSummaryPathCandidatesForExpectedDoc({
			expectedDocPath: entry.expectedDocPath,
			docsDir: configuredDocsDir,
			rootSummaryFile,
		})) {
			scopedSummaryCandidates.add(scopedSummaryPath);
		}
	}

	if (scopedSummaryCandidates.size === 0) {
		scopedSummaryCandidates.add(rootSummaryFile);
	}

	const boundedScopedIndexContext = (await Promise.all(
		[...scopedSummaryCandidates]
			.sort((left, right) => left.localeCompare(right))
			.slice(0, MAX_DIRECT_SCOPED_INDEX_CONTEXT_FILES)
			.map(async (indexPath) => ({
				path: indexPath,
				contents: await readOptionalTextFile(path.resolve(workspaceRoot, indexPath)),
			}))
	)).map((entry) => ({
		path: entry.path,
		contents: entry.contents ? truncateText(entry.contents, MAX_DIRECT_FILE_CHARS) : undefined,
	}));

	const directPromptMarkdown = [
		'AI Dev direct task: review-changed-docs',
		'',
		`Workspace: ${normalizePathForMarkdown(workspaceRoot)}`,
		`Reviewable changed files: ${normalizedChangedFilePaths.length}`,
		`Ignored non-source artifact changes: ${ignoredArtifactChangeCount}`,
		`Deterministic findings count: ${deterministicFindings.length}`,
		'',
		'Workflow file:',
		formatRelativePath(workspaceRoot, workflowFilePath),
		'',
		'```markdown',
		workflowFileContents,
		'```',
		'',
		'Finding template:',
		formatRelativePath(workspaceRoot, findingTemplatePath),
		'',
		'```markdown',
		findingTemplateContents,
		'```',
		'',
		'.ai-dev.yaml:',
		aiDevYamlSection.label,
		'',
		'```yaml',
		aiDevYamlContents,
		'```',
		'',
		'Files in review:',
		...normalizedChangedFilePaths.map((filePath) => `- ${filePath}`),
		'',
		`Changed file git diff sample (bounded to ${MAX_DIRECT_DIFF_SAMPLE_FILES} files):`,
		...boundedGitDiffs.flatMap((item) => [
			'',
			`File: ${item.relativePath}`,
			'```diff',
			item.diff.length > 0 ? item.diff : '[no diff output available]',
			'```',
		]),
		'',
		deterministicFindingsMarkdown,
		'',
		`Changed file contents sample (bounded to ${MAX_DIRECT_CHANGED_FILE_CONTENTS} files):`,
		...boundedChangedFilesWithContent.flatMap((file) => [
			'',
			`File: ${file.relativePath}`,
			'```text',
			file.contents,
			'```',
		]),
		'',
		`Expected summary context for changed source files (bounded to ${MAX_DIRECT_EXPECTED_DOC_CONTEXT_FILES} files):`,
		...expectedDocumentationContext.flatMap((item) => [
			'',
			`Source file: ${item.sourcePath}`,
			`Expected summary path: ${item.expectedDocPath}`,
			item.expectedDocContents ? 'Expected summary contents:' : 'Expected summary contents: not found',
			...(item.expectedDocContents
				? [
					'```markdown',
					item.expectedDocContents,
					'```',
				]
				: []),
		]),
		'',
		`Relevant scoped summary context (bounded to ${MAX_DIRECT_SCOPED_INDEX_CONTEXT_FILES} files):`,
		...boundedScopedIndexContext.flatMap((item) => [
			'',
			`Summary file: ${item.path}`,
			item.contents ? 'Summary contents:' : 'Summary contents: not found',
			...(item.contents
				? [
					'```markdown',
					item.contents,
					'```',
				]
				: []),
		]),
		'',
		'Instructions:',
		'Do not return an empty response.',
		'Always return either: (a) one or more findings using the finding template style, or (b) one explicit "No documentation changes required" finding with severity info.',
		'If deterministic mapping findings are empty, you must still review the changed files and expected summary context.',
		'If no documentation issues are found, emit an explicit info-severity finding that no documentation changes are required.',
		'If changes appear comment-only or non-semantic, state that docs do not need regeneration and cite the diff evidence.',
		'Deterministic mapping findings are mandatory context and must be reflected alongside your model analysis.',
		'Review changed files and related documentation for stale docs, missing docs, poor routing, and risky drift.',
		'Return findings in markdown using the provided finding template style.',
	].join('\n');

	return {
		directPromptMarkdown,
		changedFilePaths: normalizedChangedFilePaths,
		deterministicFindings,
		gitDiffs: boundedGitDiffs,
	};
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

async function previewSummarizationTarget(
	target: string
): Promise<{
	target: string;
	matchedSourceCount: number;
	plannedSummaryTargets: string[];
	previewSourcePaths: string[];
	omittedSourceCount: number;
	warnings: string[];
}> {
	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);

	const selection = await computeBatchUnitDocSelection({
		workspaceRoot,
		aiDevConfig,
		formState: {
			sourceGlob: target,
			missingDocsOnly: false,
			resolveOrphanedDocs: false,
			maxFiles: MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
			selectionMode: 'workspace',
		},
	});

	const sourcePaths = selection.plannedActions
		.filter(
			(action) =>
				action.actionType === 'generate-doc'
				&& typeof action.sourcePath === 'string'
		)
		.map((action) => action.sourcePath as string)
		.sort((left, right) => left.localeCompare(right));

	const plannedSummaryTargets = [
		...new Set(
			selection.plannedActions
				.filter(
					(action) =>
						action.actionType === 'generate-doc'
				)
				.map((action) => action.docPath)
		),
	].sort((left, right) => left.localeCompare(right));

	const warnings: string[] = [];

	if (
		selection.counts.afterGlobFilter
		> MAX_BATCH_UNIT_DOC_FILES_THIS_PASS
	) {
		warnings.push(
			`Preview was capped at ${MAX_BATCH_UNIT_DOC_FILES_THIS_PASS} source files.`
		);
	}

	return {
		target: selection.normalizedSourceGlob,
		matchedSourceCount:
			selection.counts.afterGlobFilter,
		plannedSummaryTargets,
		previewSourcePaths: sourcePaths.slice(0, 10),
		omittedSourceCount: Math.max(
			0,
			selection.counts.afterGlobFilter - 10
		),
		warnings,
	};
}

async function refreshArchitectureSummary(params: {
	workspaceRoot: string;
	aiDevConfig: AiDevConfig;
	cancellationToken: vscode.CancellationToken;
	sendPrompt(
		prompt: string,
		cancellationToken: vscode.CancellationToken
	): Promise<string>;
}): Promise<{
	path: string;
	updated: boolean;
	skipped?: string;
	failed?: string;
}> {
	const {
		workspaceRoot,
		aiDevConfig,
		cancellationToken,
		sendPrompt,
	} = params;

	const docsDir = getConfiguredDocsDir(aiDevConfig);
	const docsDirAbsolutePath = path.resolve(
		workspaceRoot,
		docsDir
	);
	const architectureSummaryPath =
		getArchitectureSummaryPath(aiDevConfig);
	const architectureSummaryAbsolutePath = path.resolve(
		workspaceRoot,
		architectureSummaryPath
	);

	if (
		!isPathInsideDirectory(
			architectureSummaryAbsolutePath,
			docsDirAbsolutePath
		)
	) {
		return {
			path: architectureSummaryPath,
			updated: false,
			skipped:
				'architecture summary is outside documentation.docsDir',
		};
	}

	if (cancellationToken.isCancellationRequested) {
		return {
			path: architectureSummaryPath,
			updated: false,
			skipped: 'cancelled before architecture refresh',
		};
	}

	try {
		const preview = await discoverArchitectureSummaryPreview({
			workspaceRoot,
			aiDevConfig,
		});

		const selectedItems = preview.items.filter(
			(item) => item.status !== 'missing'
		);

		if (selectedItems.length === 0) {
			return {
				path: architectureSummaryPath,
				updated: false,
				skipped:
					'no directory summaries are available for architecture refresh',
			};
		}

		const aiDevCorePath = resolveAiDevCorePath(
			workspaceRoot,
			aiDevConfig.aiDevCorePath
		);
		const workflowAbsolutePath = path.join(
			aiDevCorePath,
			'workflows/generate-docs/generate-architecture-summary.md'
		);
		const templateAbsolutePath = path.join(
			aiDevCorePath,
			'workflows/generate-docs/templates/architecture-summary.md'
		);
		const aiDevYamlSection =
			getAiDevYamlPromptSection(aiDevConfig);

		const [
			workflowFileContents,
			templateFileContents,
			existingArchitectureSummaryContents,
		] = await Promise.all([
			fs.readFile(workflowAbsolutePath, 'utf8'),
			fs.readFile(templateAbsolutePath, 'utf8'),
			readOptionalTextFile(
				architectureSummaryAbsolutePath
			),
		]);

		const selectedDirectories = await Promise.all(
			selectedItems.map(async (item) => {
				const summaryAbsolutePath = path.resolve(
					workspaceRoot,
					item.summaryPath
				);
				const summaryContents =
					await readOptionalTextFile(
						summaryAbsolutePath
					);

				return {
					sourceDirectory: item.sourceDirectory,
					summaryPath: item.summaryPath,
					summaryStatus: item.status,
					summaryContents:
						summaryContents !== undefined
							? truncateText(
								summaryContents,
								MAX_DIRECT_FILE_CHARS
							)
							: undefined,
				};
			})
		);

		const prompt =
			buildGenerateArchitectureSummaryDirectPromptMarkdown({
				workspaceRoot,
				workflowFilePath: formatRelativePath(
					workspaceRoot,
					workflowAbsolutePath
				),
				workflowFileContents,
				templateFilePath: formatRelativePath(
					workspaceRoot,
					templateAbsolutePath
				),
				templateFileContents,
				aiDevYamlLabel: aiDevYamlSection.label,
				aiDevYamlContents: aiDevYamlSection.contents,
				docsDir,
				targetArchitecturePath:
					architectureSummaryPath,
				existingArchitectureSummaryContents:
					existingArchitectureSummaryContents
						? truncateText(
							existingArchitectureSummaryContents,
							MAX_DIRECT_FILE_CHARS
						)
						: undefined,
				selectedDirectories,
				omittedDirectories: preview.items
					.filter(
						(item) => item.status === 'missing'
					)
					.map((item) => ({
						sourceDirectory:
							item.sourceDirectory,
						summaryPath: item.summaryPath,
						summaryStatus: item.status,
					})),
			});

		const rawResponse = await sendPrompt(
			prompt,
			cancellationToken
		);

		if (cancellationToken.isCancellationRequested) {
			return {
				path: architectureSummaryPath,
				updated: false,
				skipped:
					'cancelled during architecture refresh',
			};
		}

		const cleanedResponse =
			stripSingleOuterCodeFence(rawResponse);

		if (!cleanedResponse.text.trim()) {
			return {
				path: architectureSummaryPath,
				updated: false,
				skipped:
					'architecture model returned an empty response',
			};
		}

		await fs.mkdir(
			path.dirname(architectureSummaryAbsolutePath),
			{ recursive: true }
		);
		await writeTextFileAtomicish(
			architectureSummaryAbsolutePath,
			cleanedResponse.text
		);

		return {
			path: architectureSummaryPath,
			updated: true,
		};
	} catch (error) {
		return {
			path: architectureSummaryPath,
			updated: false,
			failed:
				error instanceof Error
					? error.message
					: String(error),
		};
	}
}

async function executeSummarizationTarget(
	target: string,
	options: {
		cancellationToken: vscode.CancellationToken;
		sendPrompt(
			prompt: string,
			cancellationToken: vscode.CancellationToken
		): Promise<string>;
		onProgress(progress: {
			completedModelCalls: number;
			totalModelCalls: number;
			outputPath: string;
		}): void;
	}
): Promise<{
	matchedSourceCount: number;
	plannedModelCalls: number;
	completedModelCalls: number;
	updatedSummaryPaths: string[];
	architectureSummaryPath?: string;
	architectureUpdated: boolean;
	architectureSkipped?: string;
	architectureFailed?: string;
	skipped: string[];
	failed: string[];
	cancelled: boolean;
}> {
	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	const modeResolution = getExecutionModeFromConfig(aiDevConfig);

	if ('errorMessage' in modeResolution) {
		throw new Error(modeResolution.errorMessage);
	}

	if (modeResolution.mode !== 'direct-experimental') {
		throw new Error(
			'/summarize in the terminal requires direct-experimental mode.'
		);
	}

	const selection = await computeBatchUnitDocSelection({
		workspaceRoot,
		aiDevConfig,
		formState: {
			sourceGlob: target,
			missingDocsOnly: false,
			resolveOrphanedDocs: false,
			maxFiles: MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
			selectionMode: 'workspace',
		},
	});

	const generateActions = selection.plannedActions.filter(
		(action) =>
			action.actionType === 'generate-doc'
			&& typeof action.sourcePath === 'string'
	);

	const groupedActions = new Map<
		string,
		BatchUnitDocPlanAction[]
	>();

	for (const action of generateActions) {
		const existing = groupedActions.get(action.docPath);

		if (existing) {
			existing.push(action);
		} else {
			groupedActions.set(action.docPath, [action]);
		}
	}

	const architectureRefreshPlanned =
		groupedActions.size > 0;

	const result = {
		matchedSourceCount: selection.counts.afterGlobFilter,
		plannedModelCalls:
			groupedActions.size
			+ (architectureRefreshPlanned ? 1 : 0),
		completedModelCalls: 0,
		updatedSummaryPaths: [] as string[],
		architectureSummaryPath: undefined as
			| string
			| undefined,
		architectureUpdated: false,
		architectureSkipped: undefined as
			| string
			| undefined,
		architectureFailed: undefined as
			| string
			| undefined,
		skipped: [] as string[],
		failed: [] as string[],
		cancelled: false,
	};

	if (groupedActions.size === 0) {
		return result;
	}

	let shouldWrite = allowDocsDirWritesForSession;

	if (!shouldWrite) {
		const writeChoice = await vscode.window.showInformationMessage(
			`Generate and write ${groupedActions.size} summary file(s)?`,
			{ modal: true },
			'Write Once',
			'Allow docsDir writes this session',
			'Cancel'
		);

		if (writeChoice === 'Write Once') {
			shouldWrite = true;
		} else if (
			writeChoice === 'Allow docsDir writes this session'
		) {
			allowDocsDirWritesForSession = true;
			shouldWrite = true;
		}
	}

	if (!shouldWrite) {
		result.cancelled = true;
		return result;
	}

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const docsDirAbsolutePath = path.resolve(
		workspaceRoot,
		configuredDocsDir
	);
	const aiDevCorePath = resolveAiDevCorePath(
		workspaceRoot,
		aiDevConfig.aiDevCorePath
	);
	const workflowAbsolutePath = path.join(
		aiDevCorePath,
		'workflows/generate-docs/generate-unit-doc.md'
	);
	const templateAbsolutePath = path.join(
		aiDevCorePath,
		'workflows/generate-docs/templates/unit-doc.md'
	);

	const [
		workflowFileContents,
		templateFileContents,
		summarizationConfig,
	] = await Promise.all([
		fs.readFile(workflowAbsolutePath, 'utf8'),
		fs.readFile(templateAbsolutePath, 'utf8'),
		readSummarizationConfig(workspaceRoot),
	]);

	const workflowFilePath = formatRelativePath(
		workspaceRoot,
		workflowAbsolutePath
	);
	const templateFilePath = formatRelativePath(
		workspaceRoot,
		templateAbsolutePath
	);

	let modelCallNumber = 0;

	for (const [docPath, actions] of groupedActions) {
		if (options.cancellationToken.isCancellationRequested) {
			result.cancelled = true;
			break;
		}

		modelCallNumber += 1;

		options.onProgress({
			completedModelCalls: modelCallNumber - 1,
			totalModelCalls: result.plannedModelCalls,
			outputPath: docPath,
		});

		const sourcePaths = [
			...new Set(
				actions
					.map((action) => action.sourcePath)
					.filter(
						(sourcePath): sourcePath is string =>
							typeof sourcePath === 'string'
							&& sourcePath.trim().length > 0
					)
					.map((sourcePath) =>
						normalizePathForMarkdown(sourcePath)
					)
			),
		].sort((left, right) => left.localeCompare(right));

		if (sourcePaths.length === 0) {
			result.skipped.push(
				`${docPath}: no source files were selected`
			);
			continue;
		}

		const outputAbsolutePath = path.resolve(
			workspaceRoot,
			docPath
		);

		if (
			!isPathInsideDirectory(
				outputAbsolutePath,
				docsDirAbsolutePath
			)
		) {
			result.skipped.push(
				`${docPath}: output is outside documentation.docsDir`
			);
			continue;
		}

		try {
			const selectedSourceFiles: Array<{
				path: string;
				contents: string;
			}> = [];

			for (const sourcePath of sourcePaths) {
				const sourceAbsolutePath = path.resolve(
					workspaceRoot,
					sourcePath
				);

				const expectedSummaryPath =
					getExpectedDirectorySummaryPath({
						workspaceRoot,
						sourceFilePath: sourceAbsolutePath,
						docsDir: configuredDocsDir,
					});

				if (expectedSummaryPath !== docPath) {
					throw new Error(
						`Expected ${sourcePath} to map to ${expectedSummaryPath}, not ${docPath}.`
					);
				}

				const sourceContents = await fs.readFile(
					sourceAbsolutePath,
					'utf8'
				);

				selectedSourceFiles.push({
					path: sourcePath,
					contents: truncateText(
						sourceContents,
						MAX_DIRECT_FILE_CHARS
					),
				});
			}

			const existingSummaryContents =
				await readOptionalTextFile(outputAbsolutePath);

			const basePrompt =
				buildGroupedGenerateUnitDocDirectPromptMarkdown({
					workspaceRoot,
					workflowFilePath,
					workflowFileContents,
					templateFilePath,
					templateFileContents,
					targetSummaryPath: docPath,
					existingSummaryContents:
						existingSummaryContents
							? truncateText(
								existingSummaryContents,
								MAX_DIRECT_FILE_CHARS
							)
							: undefined,
					selectedSourceFiles,
				});

			const resolvedBySource = sourcePaths.map(
				(sourcePath) =>
					resolveSummarizationInstructions(
						summarizationConfig,
						sourcePath
					)
			);

			const generalInstructions =
				resolvedBySource[0]?.generalInstructions ?? '';

			const matchingRules = [
				...new Map(
					resolvedBySource
						.flatMap(
							(resolved) =>
								resolved.matchingRules
						)
						.map((rule) => [
							JSON.stringify(rule),
							rule,
						])
				).values(),
			];

			const specializedInstructionSections = [
				...new Set(
					resolvedBySource
						.map((resolved) => {
							const combined =
								resolved.combinedInstructions.trim();
							const general =
								resolved.generalInstructions.trim();

							if (
								general
								&& combined.startsWith(general)
							) {
								return combined
									.slice(general.length)
									.trim();
							}

							return combined;
						})
						.filter(
							(instructions) =>
								instructions.length > 0
						)
				),
			];

			const resolvedInstructions = {
				generalInstructions,
				matchingRules,
				combinedInstructions: [
					generalInstructions.trim(),
					...specializedInstructionSections,
				]
					.filter(
						(instructions) =>
							instructions.length > 0
					)
					.join('\n\n'),
			};

			const prompt = injectSummarizationInstructions(
				basePrompt,
				resolvedInstructions
			);

			const rawResponse = await options.sendPrompt(
				prompt,
				options.cancellationToken
			);

			if (
				options.cancellationToken.isCancellationRequested
			) {
				result.cancelled = true;
				break;
			}

			const cleanedResponse =
				stripSingleOuterCodeFence(rawResponse);

			if (!cleanedResponse.text.trim()) {
				result.skipped.push(
					`${docPath}: model returned an empty response`
				);
				continue;
			}

			await fs.mkdir(
				path.dirname(outputAbsolutePath),
				{ recursive: true }
			);
			await writeTextFileAtomicish(
				outputAbsolutePath,
				cleanedResponse.text
			);

			result.completedModelCalls += 1;
			result.updatedSummaryPaths.push(docPath);
		} catch (error) {
			if (
				options.cancellationToken.isCancellationRequested
			) {
				result.cancelled = true;
				break;
			}

			const message =
				error instanceof Error
					? error.message
					: String(error);

			result.failed.push(`${docPath}: ${message}`);
		}
	}

	if (
		result.updatedSummaryPaths.length > 0
		&& !result.cancelled
		&& !options.cancellationToken.isCancellationRequested
	) {
		options.onProgress({
			completedModelCalls: groupedActions.size,
			totalModelCalls: result.plannedModelCalls,
			outputPath:
				getArchitectureSummaryPath(aiDevConfig),
		});

		const architecture =
			await refreshArchitectureSummary({
				workspaceRoot,
				aiDevConfig,
				cancellationToken:
					options.cancellationToken,
				sendPrompt: options.sendPrompt,
			});

		result.architectureSummaryPath =
			architecture.path;
		result.architectureUpdated =
			architecture.updated;
		result.architectureSkipped =
			architecture.skipped;
		result.architectureFailed =
			architecture.failed;

		if (architecture.updated) {
			result.completedModelCalls += 1;
		}
	} else if (
		architectureRefreshPlanned
		&& result.updatedSummaryPaths.length === 0
	) {
		result.architectureSkipped =
			'no directory summaries were updated';
	}

	return result;
}

async function prepareSingleFileSummary(
	target: string
): Promise<{
	prompt: string;
	sourcePath: string;
	outputPath: string;
	warnings: string[];
}> {
	const workspaceRoot = getOpenWorkspaceRoot();
	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const sourceAbsolutePath = path.resolve(
		workspaceRoot,
		target
	);

	if (!isPathInsideDirectory(sourceAbsolutePath, workspaceRoot)) {
		throw new Error(
			'The summarize target must be inside the open workspace.'
		);
	}

	let sourceStat;
	try {
		sourceStat = await fs.stat(sourceAbsolutePath);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
			throw new Error(`Source file not found: ${target}`);
		}

		throw error;
	}

	if (!sourceStat.isFile()) {
		throw new Error(
			`Single-file summarization requires a file: ${target}`
		);
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	const modeResolution = getExecutionModeFromConfig(aiDevConfig);

	if ('errorMessage' in modeResolution) {
		throw new Error(modeResolution.errorMessage);
	}

	if (modeResolution.mode !== 'direct-experimental') {
		throw new Error(
			'/summarize in the terminal requires direct-experimental mode.'
		);
	}

	const bundle = await buildGenerateUnitDocDirectPromptBundle({
		workspaceRoot,
		activeFileUri: vscode.Uri.file(sourceAbsolutePath),
		aiDevConfig,
	});

	const summarizationConfig =
		await readSummarizationConfig(workspaceRoot);
	const resolvedInstructions =
		resolveSummarizationInstructions(
			summarizationConfig,
			bundle.selectedSourcePath
		);

	return {
		prompt: injectSummarizationInstructions(
			bundle.directPromptMarkdown,
			resolvedInstructions
		),
		sourcePath: bundle.selectedSourcePath,
		outputPath: bundle.expectedSummaryPath,
		warnings: [],
	};
}

async function completeSingleFileSummary(
	preparation: {
		prompt: string;
		sourcePath: string;
		outputPath: string;
		warnings: string[];
	},
	rawResponseText: string
): Promise<{
	written: boolean;
	outputPath: string;
	warnings: string[];
}> {
	const workspaceRoot = getOpenWorkspaceRoot();
	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const docsDirAbsolutePath = path.resolve(
		workspaceRoot,
		configuredDocsDir
	);
	const outputAbsolutePath = path.resolve(
		workspaceRoot,
		preparation.outputPath
	);

	if (
		!isPathInsideDirectory(
			outputAbsolutePath,
			docsDirAbsolutePath
		)
	) {
		throw new Error(
			`Expected output ${preparation.outputPath} is outside documentation.docsDir (${configuredDocsDir}).`
		);
	}

	const cleanedResponse =
		stripSingleOuterCodeFence(rawResponseText);
	const responseText = cleanedResponse.text;

	if (!responseText.trim()) {
		return {
			written: false,
			outputPath: preparation.outputPath,
			warnings: [
				'The model returned an empty response; no file was written.',
			],
		};
	}

	let shouldWrite = allowDocsDirWritesForSession;

	if (!allowDocsDirWritesForSession) {
		const writeChoice =
			await vscode.window.showInformationMessage(
				`Write generated documentation to ${preparation.outputPath}?`,
				{ modal: true },
				'Write Once',
				'Allow docsDir writes this session',
				'Preview Only'
			);

		if (writeChoice === 'Write Once') {
			shouldWrite = true;
		} else if (
			writeChoice
			=== 'Allow docsDir writes this session'
		) {
			allowDocsDirWritesForSession = true;
			shouldWrite = true;
		}
	}

	if (!shouldWrite) {
		return {
			written: false,
			outputPath: preparation.outputPath,
			warnings: cleanedResponse.stripped
				? ['Removed an outer Markdown code fence from the model response.']
				: [],
		};
	}

	await fs.mkdir(
		path.dirname(outputAbsolutePath),
		{ recursive: true }
	);
	await writeTextFileAtomicish(
		outputAbsolutePath,
		responseText
	);

	return {
		written: true,
		outputPath: preparation.outputPath,
		warnings: cleanedResponse.stripped
			? ['Removed an outer Markdown code fence from the model response.']
			: [],
	};
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

interface ResolvedCodeReviewSelection {
	aiDevConfig: AiDevConfig;
	changedFilePaths: string[];
	implementationPaths: string[];
	testPaths: string[];
	selectedPaths: string[];
	selectedChangedPaths: string[];
	warnings: string[];
}

async function resolveCodeReviewSelection(params: {
	workspaceRoot: string;
	request: AssistantReviewRequest;
}): Promise<ResolvedCodeReviewSelection> {
	const {
		workspaceRoot,
		request,
	} = params;

	const {
		mode,
		target,
		includeAllMatches,
	} = request;

	if (mode === 'docs') {
		throw new Error(
			'Code and test review selection does not support documentation mode.'
		);
	}

	const aiDevConfig =
		await readAiDevConfig(workspaceRoot);

	const docsDir =
		getConfiguredDocsDir(aiDevConfig);

	const changedFilePaths =
		await getGitChangedFiles(workspaceRoot);

	const candidateFilePaths =
		includeAllMatches
			? (
				await discoverBatchUnitDocCandidates(
					workspaceRoot,
					aiDevConfig
				)
			).map((absolutePath) =>
				normalizePathForMarkdown(
					path.relative(
						workspaceRoot,
						absolutePath
					)
				)
			)
			: changedFilePaths;

	const selection = selectReviewFiles({
		candidateFilePaths,
		mode,
		docsDir,
		target,
	});

	const {
		implementationPaths,
		testPaths,
		selectedPaths,
	} = selection;

	if (selectedPaths.length === 0) {
		const scopeDescription =
			target
				? ` matching ${target}`
				: '';

		throw new Error(
			includeAllMatches
				? mode === 'code'
					? `No implementation files found${scopeDescription}.`
					: `No implementation or test files found${scopeDescription}.`
				: mode === 'code'
					? `No changed implementation files found${scopeDescription}.`
					: `No changed implementation or test files found${scopeDescription}.`
		);
	}

	const changedPathSet = new Set(
		changedFilePaths.map((filePath) =>
			normalizePathForMarkdown(filePath)
		)
	);

	const selectedChangedPaths =
		selectedPaths.filter((filePath) =>
			changedPathSet.has(filePath)
		);

	const warnings: string[] = [];

	if (includeAllMatches) {
		warnings.push(
			target
				? `Included unchanged files matching ${target}.`
				: 'Included unchanged files across the project.'
		);
	}

	if (selectedPaths.length > 100) {
		warnings.push(
			`Large review scope: ${selectedPaths.length} files selected.`
		);
	}

	return {
		aiDevConfig,
		changedFilePaths,
		implementationPaths,
		testPaths,
		selectedPaths,
		selectedChangedPaths,
		warnings,
	};
}

async function buildChangedCodeReviewPrompt(params: {
	workspaceRoot: string;
	request: AssistantReviewRequest;
}): Promise<{
	prompt: string;
	changedFilePaths: string[];
	warnings: string[];
}> {
	const {
		workspaceRoot,
		request,
	} = params;

	const {
		mode,
		includeAllMatches,
	} = request;

	if (mode === 'docs') {
		throw new Error(
			'Code and test review builder does not support documentation mode.'
		);
	}

	const selection =
		await resolveCodeReviewSelection({
			workspaceRoot,
			request,
		});

	const {
		aiDevConfig,
		implementationPaths,
		testPaths,
		selectedPaths,
		selectedChangedPaths,
	} = selection;

	const warnings = [...selection.warnings];

	const [
		filesWithContent,
		gitDiffs,
		findingTemplateContents,
	] = await Promise.all([
		existingChangedFilesWithContent(
			workspaceRoot,
			selectedPaths
		),
		getGitDiffForFiles(
			workspaceRoot,
			selectedChangedPaths
		),
		fs.readFile(
			path.join(
				resolveAiDevCorePath(
					workspaceRoot,
					aiDevConfig.aiDevCorePath
				),
				'workflows/review/finding-template.md'
			),
			'utf8'
		),
	]);

	const boundedFiles = filesWithContent
		.slice(0, MAX_DIRECT_CHANGED_FILE_CONTENTS)
		.map((file) => ({
			relativePath: file.relativePath,
			contents: truncateText(
				file.contents,
				MAX_DIRECT_FILE_CHARS
			),
		}));

	const boundedDiffs = gitDiffs
		.slice(0, MAX_DIRECT_DIFF_SAMPLE_FILES)
		.map((item) => ({
			relativePath:
				normalizePathForMarkdown(
					item.relativePath
				),
			diff: truncateText(
				item.diff,
				MAX_DIRECT_DIFF_CHARS
			),
		}));

	const codeInstructions = [
		'Review implementation for correctness defects, unsafe edge cases, broken lifecycle behavior, regressions, misleading state transitions, and maintainability risks.',
		'Prioritize concrete defects supported by the supplied source and diffs.',
		'Do not create findings for style preferences or speculative redesigns.',
		'Use blocking only for issues likely to cause severe breakage or data loss.',
		'Use warning for credible defects or meaningful regression risks.',
		'Use info sparingly for actionable low-risk observations.',
	];

	const testInstructions = [
		'Review whether the supplied implementation has adequate regression coverage.',
		'Identify missing, weak, misleading, or obsolete tests.',
		'Check important success, failure, cancellation, parsing, and state-transition paths.',
		'Do not request tests for trivial formatting-only or type-only changes.',
		'Use the Source file field for the implementation file.',
		'Use the Documentation file field for the relevant test file, or none when a test is missing.',
		'All available paths, source contents, test contents, and diffs are embedded below.',
		'Do not ask the user to open files, attach files, paste code, select an editor, or provide additional context.',
		'Assess the supplied material directly.',
		'Return at least one structured finding using the provided template.',
	];

	const modeInstructions =
		mode === 'code'
			? codeInstructions
			: testInstructions;

	const prompt = [
		`AI Dev direct task: review-${includeAllMatches ? 'all' : 'changed'}-${mode}`,
		'',
		`Workspace: ${normalizePathForMarkdown(workspaceRoot)}`,
		`Implementation files in review: ${implementationPaths.length}`,
		`Test files in review: ${testPaths.length}`,
		`Changed files in review: ${selectedChangedPaths.length}`,
		'',
		'Finding template:',
		'```markdown',
		findingTemplateContents,
		'```',
		'',
		'Files in review:',
		...selectedPaths.map(
			(filePath) => `- ${filePath}`
		),
		'',
		'Git diff samples:',
		...boundedDiffs.flatMap((item) => [
			'',
			`File: ${item.relativePath}`,
			'```diff',
			item.diff || '[no diff output available]',
			'```',
		]),
		'',
		'File contents:',
		...boundedFiles.flatMap((file) => [
			'',
			`File: ${file.relativePath}`,
			'```text',
			file.contents,
			'```',
		]),
		'',
		'Instructions:',
		...modeInstructions,
		'Return findings only.',
		'Use the provided finding structure exactly.',
		'For non-documentation reviews, Category may be Correctness, Reliability, Maintainability, Security, Performance, or Test coverage.',
		'If no actionable issue is found, return one info-severity finding explaining that no changes are required.',
	].join('\n');

	if (
		selectedPaths.length
		> MAX_DIRECT_CHANGED_FILE_CONTENTS
	) {
		warnings.push(
			`Model context includes file contents for the first ${MAX_DIRECT_CHANGED_FILE_CONTENTS} of ${selectedPaths.length} selected files.`
		);
	}

	if (
		selectedChangedPaths.length
		> MAX_DIRECT_DIFF_SAMPLE_FILES
	) {
		warnings.push(
			`Model context includes diff samples for the first ${MAX_DIRECT_DIFF_SAMPLE_FILES} of ${selectedChangedPaths.length} changed files in scope.`
		);
	}

	return {
		prompt,
		changedFilePaths: selectedPaths,
		warnings,
	};
}

async function previewProjectReview(
	request: AssistantReviewRequest
): Promise<{
	mode: 'docs' | 'code' | 'tests';
	target?: string;
	includeAllMatches: boolean;
	implementationFileCount: number;
	testFileCount: number;
	selectedFileCount: number;
	changedFileCount: number;
	previewFilePaths: string[];
	omittedFileCount: number;
	warnings: string[];
}> {
	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	if (request.mode === 'docs') {
		throw new Error(
			'Documentation review smoke tests are not connected yet.'
		);
	}

	const selection =
		await resolveCodeReviewSelection({
			workspaceRoot,
			request,
		});

	const previewFilePaths =
		selection.selectedPaths.slice(0, 10);

	return {
		mode: request.mode,
		target: request.target,
		includeAllMatches:
			request.includeAllMatches,
		implementationFileCount:
			selection.implementationPaths.length,
		testFileCount:
			selection.testPaths.length,
		selectedFileCount:
			selection.selectedPaths.length,
		changedFileCount:
			selection.selectedChangedPaths.length,
		previewFilePaths,
		omittedFileCount:
			Math.max(
				0,
				selection.selectedPaths.length
					- previewFilePaths.length
			),
		warnings: selection.warnings,
	};
}

async function prepareProjectReview(
	request: AssistantReviewRequest
): Promise<{
	mode: 'docs' | 'code' | 'tests';
	prompt: string;
	changedFileCount: number;
	deterministicFindingCount: number;
	deterministicFindingsMarkdown: string;
	warnings: string[];
}> {
	const {
		mode,
		target,
		includeAllMatches,
	} = request;

	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	const modeResolution = getExecutionModeFromConfig(aiDevConfig);

	if ('errorMessage' in modeResolution) {
		throw new Error(modeResolution.errorMessage);
	}

	if (modeResolution.mode !== 'direct-experimental') {
		throw new Error(
			'/review in the terminal requires direct-experimental mode.'
		);
	}

	if (
		mode === 'docs'
		&& (target || includeAllMatches)
	) {
		throw new Error(
			'Targeted and --all documentation review are not connected yet.'
		);
	}

	if (mode === 'docs') {
		const bundle =
			await buildReviewDocumentationDirectPromptBundle(
				workspaceRoot,
				aiDevConfig
			);

		return {
			mode,
			prompt: bundle.directPromptMarkdown,
			changedFileCount: bundle.changedFilePaths.length,
			deterministicFindingCount:
				bundle.deterministicFindings.length,
			deterministicFindingsMarkdown:
				toDeterministicFindingsMarkdown(
					bundle.deterministicFindings
				),
			warnings: [],
		};
	}

	const bundle = await buildChangedCodeReviewPrompt({
		workspaceRoot,
		request,
	});

	return {
		mode,
		prompt: bundle.prompt,
		changedFileCount: bundle.changedFilePaths.length,
		deterministicFindingCount: 0,
		deterministicFindingsMarkdown: [
			'## Deterministic Documentation Mapping Findings',
			'',
			'- not applicable',
		].join('\n'),
		warnings: bundle.warnings,
	};
}


async function buildSummaryAnswerRoute(
	userQuestion: string
): Promise<{
	prompt: string;
	warnings: string[];
}> {
	const workspaceRoot = getOpenWorkspaceRoot();
	if (!workspaceRoot) {
		throw new Error('No workspace is open.');
	}

	const trimmedUserQuestion = userQuestion.trim();
	if (!trimmedUserQuestion) {
		throw new Error('A question is required.');
	}

	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	const aiDevCorePath = resolveAiDevCorePath(
		workspaceRoot,
		aiDevConfig.aiDevCorePath
	);
	const aiDevYamlSection = getAiDevYamlPromptSection(aiDevConfig);
	const workflowFilePath = path.join(
		aiDevCorePath,
		'workflows/answer-docs/answer-from-ai-docs.md'
	);
	const docsDir = getConfiguredDocsDir(aiDevConfig);
	const docsDirAbsolutePath = path.resolve(workspaceRoot, docsDir);
	const architectureRootSummaryPath = getRootSummaryFilePath(aiDevConfig);
	const architectureRootSummaryAbsolutePath = path.resolve(
		workspaceRoot,
		architectureRootSummaryPath
	);
	const legacyRootSummaryPath = getLegacyRootSummaryFilePath(aiDevConfig);
	const legacyRootSummaryAbsolutePath = path.resolve(
		workspaceRoot,
		legacyRootSummaryPath
	);

	const [
		workflowFileContents,
		architectureRootSummaryContents,
		legacyRootSummaryContents,
	] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		readOptionalTextFile(architectureRootSummaryAbsolutePath),
		readOptionalTextFile(legacyRootSummaryAbsolutePath),
	]);

	const architectureRootSummaryUsable =
		architectureRootSummaryContents !== undefined
		&& architectureRootSummaryContents.trim().length > 0;

	const rootSummaryPath = architectureRootSummaryUsable
		? architectureRootSummaryPath
		: legacyRootSummaryPath;
	const rootSummaryAbsolutePath = architectureRootSummaryUsable
		? architectureRootSummaryAbsolutePath
		: legacyRootSummaryAbsolutePath;
	const rootSummaryContents = architectureRootSummaryUsable
		? architectureRootSummaryContents
		: legacyRootSummaryContents;

	const rootSummaryExists = rootSummaryContents !== undefined;
	const rootSummaryEmpty =
		rootSummaryExists
		&& rootSummaryContents.trim().length === 0;

	const routedDocumentationContext =
		await collectRoutedDocumentationContextForAnswer({
			workspaceRoot,
			docsDirAbsolutePath,
			rootSummaryPath: rootSummaryAbsolutePath,
			userQuestion: trimmedUserQuestion,
		});

	const discoveredSummaryPaths =
		await discoverSummaryFilesRecursively(docsDirAbsolutePath);
	const discoveredSummaryCount = discoveredSummaryPaths.length;

	const routedSummaryAbsolutePaths = new Set(
		routedDocumentationContext.routedFiles
			.filter((file) => file.kind === 'summary')
			.map((file) => path.resolve(workspaceRoot, file.path))
	);

	const excludeFallbackAbsolutePaths = new Set<string>(
		routedSummaryAbsolutePaths
	);

	if (rootSummaryExists && !rootSummaryEmpty) {
		excludeFallbackAbsolutePaths.add(rootSummaryAbsolutePath);
	}

	excludeFallbackAbsolutePaths.add(
		architectureRootSummaryAbsolutePath
	);
	excludeFallbackAbsolutePaths.add(legacyRootSummaryAbsolutePath);

	let fallbackIncludedReason: string | undefined;

	if (!rootSummaryExists) {
		fallbackIncludedReason = 'Root summary file is missing.';
	} else if (rootSummaryEmpty) {
		fallbackIncludedReason = 'Root summary file is empty.';
	} else if (
		routedDocumentationContext.routedFiles.length === 0
	) {
		fallbackIncludedReason =
			'Root summary routing returned no additional context for this question.';
	}

	const fallbackDiscoveredSummaries = fallbackIncludedReason
		? await selectFallbackDiscoveredSummaries({
			workspaceRoot,
			discoveredSummaryPaths,
			excludeAbsolutePaths: excludeFallbackAbsolutePaths,
			userQuestion: trimmedUserQuestion,
			maxSummaries: MAX_FALLBACK_DISCOVERED_SUMMARIES,
		})
		: [];

	const prompt = buildAnswerFromAiDocsDirectPromptMarkdown({
		workspaceRoot,
		workflowFilePath,
		workflowFileContents,
		aiDevYamlLabel: aiDevYamlSection.label,
		aiDevYamlContents: aiDevYamlSection.contents,
		rootSummaryPath,
		rootSummaryExists,
		rootSummaryEmpty,
		rootSummaryContents,
		docsDir,
		discoveredSummaryCount,
		fallbackIncludedReason,
		routedDocumentationFiles:
			routedDocumentationContext.routedFiles,
		fallbackDiscoveredSummaries:
			fallbackDiscoveredSummaries.map((summary) => ({
				path: summary.relativePath,
				contents: truncateText(
					summary.contents,
					MAX_ROUTED_DOC_FILE_CHARS
				),
				score: summary.score,
			})),
		missingDocumentationPaths:
			routedDocumentationContext.missingPaths,
		userQuestion: trimmedUserQuestion,
	});

	const warnings: string[] = [];

	if (!rootSummaryExists) {
		warnings.push(
			`Root summary was not found at ${rootSummaryPath}; fallback discovery was used.`
		);
	} else if (rootSummaryEmpty) {
		warnings.push(
			`Root summary at ${rootSummaryPath} is empty; fallback discovery was used.`
		);
	}

	if (routedDocumentationContext.missingPaths.length > 0) {
		warnings.push(
			`Missing routed documentation: ${routedDocumentationContext.missingPaths.join(', ')}`
		);
	}

	if (
		rootSummaryExists
		&& !rootSummaryEmpty
		&& routedDocumentationContext.routedFiles.length === 0
	) {
		warnings.push(
			'Routing documentation did not identify additional context; fallback summaries were used.'
		);
	}

	return {
		prompt,
		warnings,
	};
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
