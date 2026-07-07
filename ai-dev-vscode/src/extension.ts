import * as vscode from 'vscode';
import * as fs from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import * as path from 'node:path';
import { registerAiDevActionsView } from './actionsView';
import { WorkflowDetailsViewProvider } from './workflowDetailsView';
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
	buildFileDocumentationReviewPromptMarkdown,
	buildGenerateArchitectureSummaryDirectPromptMarkdown,
	buildGenerateArchitectureSummaryPromptMarkdown,
	buildGroupedGenerateUnitDocDirectPromptMarkdown,
	buildReviewDocumentationPromptMarkdown,
	buildUnitDocPromptMarkdown,
} from './promptBuilder';
import {
	getActiveWorkspaceContext,
	getExpectedDirectorySummaryPath,
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
	getSelectedSourcePath,
} from './workspace';
import {
	openBatchUnitDocsWebview,
	type BatchUnitDocActionType,
	type BatchUnitDocPreviewItem,
	type BatchUnitDocPreviewResult,
	type BatchUnitDocStatus,
	type BatchUnitDocsFormState,
} from './batchUnitDocsView';
import {
	openArchitectureSummaryWebview,
	type ArchitectureSummaryPreviewItem,
	type ArchitectureSummaryPreviewResult,
	type ArchitectureSummaryStatus,
} from './architectureSummaryView';
import { openSettingsWebview } from './settingsView';

const GENERATE_UNIT_DOC_COMMAND = 'aiDev.generateUnitDocPromptForActiveFile';
const GENERATE_UNIT_DOCS_BATCH_EXPERIMENTAL_COMMAND = 'aiDev.generateUnitDocsBatchExperimental';
const GENERATE_ARCHITECTURE_SUMMARY_COMMAND = 'aiDev.generateArchitectureSummary';
const UPDATE_FOLDER_SUMMARY_COMMAND = 'aiDev.updateFolderSummary';
const REVIEW_DOCUMENTATION_COMMAND = 'aiDev.reviewChangedDocsPrompt';
const REVIEW_FILE_DOCUMENTATION_COMMAND = 'aiDev.reviewFileDocsPrompt';
const ANSWER_FROM_AI_DOCS_COMMAND = 'aiDev.answerFromAiDocsPrompt';
const COPILOT_TEST_COMMAND = 'aiDev.copilotTest';
const SELECT_WORKFLOW_COMMAND = 'aiDev.selectWorkflow';
const SETTINGS_COMMAND = 'aiDev.settings';
const SET_EXECUTION_MODE_COMMAND = 'aiDev.setExecutionMode';
const FALLBACK_SUMMARY_FILE = 'ai-docs/summary.md';
const ARCHITECTURE_SUMMARY_FILE_NAME = 'architecture-summary.md';
const DEFAULT_BATCH_UNIT_DOC_PREVIEW_LIMIT = 25;
const MAX_BATCH_UNIT_DOC_PREVIEW_LIMIT = 100;
const FALLBACK_BATCH_EXCLUDE_GLOBS = [
	'.git/**',
	'**/.git/**',
	'.*',
	'**/.*',
	'node_modules/**',
	'**/node_modules/**',
	'dist/**',
	'build/**',
	'out/**',
	'coverage/**',
	'vendor/**',
	'vendors/**',
	'libs/**',
	'Libs/**',
	'**/*.min.*',
	'**/*.generated.*',
	'**/*.lock',
];
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
	return path.posix.join(normalizedDocsDir, 'summary.md');
}

function getArchitectureSummaryPath(config: AiDevConfig): string {
	const configuredDocsDir = getConfiguredDocsDir(config).replace(/\/+$/, '');
	return path.posix.join(configuredDocsDir, ARCHITECTURE_SUMMARY_FILE_NAME);
}

function getConfiguredDocsDir(config: AiDevConfig): string {
	const configuredDocsDir = config.docsDir?.trim();
	if (!configuredDocsDir) {
		return 'ai-docs';
	}

	return normalizePathForMarkdown(configuredDocsDir);
}

function isPathInsideDirectory(targetPath: string, directoryPath: string): boolean {
	const relativePath = path.relative(directoryPath, targetPath);
	return relativePath === '' || (!relativePath.startsWith('..') && !path.isAbsolute(relativePath));
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

async function openMarkdownPromptAndCopy(content: string, message: string): Promise<void> {
	const doc = await vscode.workspace.openTextDocument({
		language: 'markdown',
		content,
	});
	await vscode.window.showTextDocument(doc, { preview: false });
	await vscode.env.clipboard.writeText(content);
	await vscode.window.showInformationMessage(message);
}

async function openMarkdownDocument(content: string): Promise<void> {
	const doc = await vscode.workspace.openTextDocument({
		language: 'markdown',
		content,
	});
	await vscode.window.showTextDocument(doc, { preview: false });
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

async function listFilesRecursively(rootDirectory: string): Promise<string[]> {
	const results: string[] = [];
	const pendingDirectories: string[] = [rootDirectory];

	while (pendingDirectories.length > 0) {
		const currentDirectory = pendingDirectories.pop();
		if (!currentDirectory) {
			continue;
		}

		const entries = await fs.readdir(currentDirectory, { withFileTypes: true, encoding: 'utf8' });
		for (const entry of entries) {
			const entryPath = path.join(currentDirectory, entry.name);
			if (entry.isDirectory()) {
				pendingDirectories.push(entryPath);
				continue;
			}

			if (entry.isFile()) {
				results.push(entryPath);
			}
		}
	}

	return results;
}

function parseYamlList(rawYaml: string, section: string, key: string): string[] {
	const lines = rawYaml.split(/\r?\n/);
	const sectionPattern = new RegExp(`^${section}:\\s*$`);
	const keyPattern = new RegExp(`^\\s{2}${key}:\\s*$`);

	let sectionIndex = -1;
	for (let index = 0; index < lines.length; index += 1) {
		if (sectionPattern.test(lines[index])) {
			sectionIndex = index;
			break;
		}
	}

	if (sectionIndex < 0) {
		return [];
	}

	let keyIndex = -1;
	for (let index = sectionIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		if (/^\S/.test(line)) {
			break;
		}

		if (keyPattern.test(line)) {
			keyIndex = index;
			break;
		}
	}

	if (keyIndex < 0) {
		return [];
	}

	const values: string[] = [];
	for (let index = keyIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		if (/^\S/.test(line)) {
			break;
		}

		if (/^\s{2}[a-zA-Z0-9_-]+:\s*/.test(line)) {
			break;
		}

		const match = line.match(/^\s{4}-\s+(.+)\s*$/);
		if (!match || !match[1]) {
			continue;
		}

		const rawValue = match[1].trim();
		if (rawValue.length === 0) {
			continue;
		}

		if (
			(rawValue.startsWith('"') && rawValue.endsWith('"'))
			|| (rawValue.startsWith('\'') && rawValue.endsWith('\''))
		) {
			values.push(rawValue.slice(1, -1));
			continue;
		}

		values.push(rawValue);
	}

	return values;
}

function globToRegExp(globPattern: string): RegExp {
	const normalizedGlob = normalizePathForMarkdown(globPattern.trim());
	let regexSource = '';

	for (let index = 0; index < normalizedGlob.length; index += 1) {
		const char = normalizedGlob[index];
		if (char === '*') {
			const isDoubleStar = normalizedGlob[index + 1] === '*';
			if (isDoubleStar) {
				const hasFollowingSlash = normalizedGlob[index + 2] === '/';
				if (hasFollowingSlash) {
					regexSource += '(?:.*/)?';
					index += 2;
				} else {
					regexSource += '.*';
					index += 1;
				}
				continue;
			}

			regexSource += '[^/]*';
			continue;
		}

		if (char === '?') {
			regexSource += '[^/]';
			continue;
		}

		if (/[-/\\^$+?.()|[\]{}]/.test(char)) {
			regexSource += `\\${char}`;
			continue;
		}

		regexSource += char;
	}

	return new RegExp(`^${regexSource}$`);
}

function matchesAnyGlob(relativePath: string, globs: string[]): boolean {
	const normalizedRelativePath = normalizePathForMarkdown(relativePath);
	return globs.some((globPattern) => globToRegExp(globPattern).test(normalizedRelativePath));
}

function getBatchSourceGlobs(config: AiDevConfig): { excludeGlobs: string[] } {
	const excludeGlobs = parseYamlList(config.raw, 'source', 'exclude');

	return {
		excludeGlobs: excludeGlobs.length > 0 ? excludeGlobs : FALLBACK_BATCH_EXCLUDE_GLOBS,
	};
}

interface DeterministicDocumentationFinding {
	title: string;
	details: string[];
	recommendation?: string;
}

function isConfiguredSourcePath(relativePath: string, excludeGlobs: string[]): boolean {
	return !matchesAnyGlob(relativePath, excludeGlobs);
}

function isConfiguredSourceCandidatePath(relativePath: string, docsDir: string, excludeGlobs: string[]): boolean {
	const normalizedRelativePath = normalizePathForMarkdown(relativePath);
	const normalizedDocsDir = normalizePathForMarkdown(docsDir).replace(/\/+$/, '');
	if (normalizedRelativePath === normalizedDocsDir || normalizedRelativePath.startsWith(`${normalizedDocsDir}/`)) {
		return false;
	}

	return isConfiguredSourcePath(normalizedRelativePath, excludeGlobs);
}

function getPathWithoutExtension(relativePath: string): string {
	const extension = path.posix.extname(relativePath);
	if (!extension) {
		return relativePath;
	}

	return relativePath.slice(0, -extension.length);
}

function inferSourceExtensionsFromSourcePaths(sourcePaths: string[]): string[] {
	const discovered = new Set<string>();
	for (const sourcePath of sourcePaths) {
		const extensionName = path.posix.extname(sourcePath).trim().toLowerCase();
		if (extensionName) {
			discovered.add(extensionName);
		}
	}

	return [...discovered].sort((left, right) => left.localeCompare(right));
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

async function discoverBatchUnitDocCandidates(workspaceRoot: string, config: AiDevConfig): Promise<string[]> {
	const { excludeGlobs } = getBatchSourceGlobs(config);
	const docsDir = getConfiguredDocsDir(config);
	const docsDirAbsolutePath = path.resolve(workspaceRoot, docsDir);
	const allFiles = await listFilesRecursively(workspaceRoot);
	const candidates = allFiles.filter((absolutePath) => {
		if (isPathInsideDirectory(absolutePath, docsDirAbsolutePath)) {
			return false;
		}

		const relativePath = normalizePathForMarkdown(path.relative(workspaceRoot, absolutePath));
		if (matchesAnyGlob(relativePath, excludeGlobs)) {
			return false;
		}

		return true;
	});

	candidates.sort((left, right) => left.localeCompare(right));
	return candidates;
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

interface BatchGeneratedSummaryWrite {
	docPath: string;
	sourcePaths: string[];
}

interface BatchSkippedAction {
	actionType: BatchUnitDocActionType;
	docPath: string;
	sourcePath?: string;
	sourcePaths?: string[];
	reason: string;
}

interface BatchFailedAction {
	actionType: BatchUnitDocActionType;
	docPath: string;
	sourcePath?: string;
	sourcePaths?: string[];
	error: string;
}

interface ArchitectureSummaryDirectoryCandidate {
	sourceDirectory: string;
	summaryPath: string;
	summaryAbsolutePath: string;
	status: ArchitectureSummaryStatus;
	notes: string;
	applyByDefault: boolean;
}

interface ArchitectureSummaryDirectorySelection {
	allItems: ArchitectureSummaryPreviewItem[];
	selectedItems: ArchitectureSummaryPreviewItem[];
	omittedItems: ArchitectureSummaryPreviewItem[];
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

function getSelectedDirectorySummaryFile(selectedSourceDirectory: string | undefined, docsDir: string): string {
	const normalizedDirectory = normalizeSelectedSourceDirectory(selectedSourceDirectory);
	return normalizedDirectory && normalizedDirectory !== '.'
		? path.posix.join(docsDir, normalizedDirectory, 'summary.md')
		: path.posix.join(docsDir, 'summary.md');
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

function selectArchitecturePreviewItems(previewPlan: ArchitectureSummaryPreviewResult): ArchitectureSummaryDirectorySelection {
	const allItems = Array.isArray(previewPlan.items)
		? previewPlan.items
			.map((item) => ({
				apply: Boolean(item.apply),
				sourceDirectory: String(item.sourceDirectory ?? '').trim(),
				summaryPath: String(item.summaryPath ?? '').trim(),
				status: item.status,
				notes: String(item.notes ?? '').trim(),
			}))
			.filter((item): item is ArchitectureSummaryPreviewItem => (
				item.sourceDirectory.length > 0
				&& item.summaryPath.length > 0
				&& (item.status === 'exists' || item.status === 'missing' || item.status === 'empty')
			))
		: [];

	const selectedItems = allItems.filter((item) => item.apply);
	const omittedItems = allItems.filter((item) => !item.apply);
	return { allItems, selectedItems, omittedItems };
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

function buildArchitectureSummaryReport(params: {
	timestamp: string;
	targetPath: string;
	docsDir: string;
	modelDetails?: DirectAnswerModelDetails;
	selectedCount: number;
	existingCount: number;
	missingCount: number;
	emptyCount: number;
	responseLength: number;
	fenceStripped: boolean;
	writeStatus: string;
	fileWritten: boolean;
	writtenFilePath?: string;
	skipped: string[];
	failed: string[];
}): string {
	return [
		'# AI Dev Generate Architecture Summary Report',
		'',
		`- Timestamp: ${params.timestamp}`,
		'- Execution mode: direct-experimental',
		`- docsDir: ${params.docsDir}`,
		`- Target path: ${params.targetPath}`,
		`- Selected directory count: ${params.selectedCount}`,
		`- Existing summaries count: ${params.existingCount}`,
		`- Missing summaries count: ${params.missingCount}`,
		`- Empty summaries count: ${params.emptyCount}`,
		`- Write status: ${params.writeStatus}`,
		`- File written: ${params.fileWritten}`,
		`- Written file path: ${params.writtenFilePath ?? 'n/a'}`,
		`- Response length: ${params.responseLength}`,
		`- Response fence stripped: ${params.fenceStripped}`,
		'- Selected model info:',
		`  - id: ${params.modelDetails?.id ?? 'n/a'}`,
		`  - name: ${params.modelDetails?.name ?? 'n/a'}`,
		`  - vendor: ${params.modelDetails?.vendor ?? 'n/a'}`,
		`  - family: ${params.modelDetails?.family ?? 'n/a'}`,
		'',
		'## Skipped',
		...(params.skipped.length > 0 ? params.skipped.map((item) => `- ${item}`) : ['- none']),
		'',
		'## Failed',
		...(params.failed.length > 0 ? params.failed.map((item) => `- ${item}`) : ['- none']),
	].join('\n');
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
	const normalizedSourceGlob = formState.sourceGlob.trim().length > 0 ? formState.sourceGlob.trim() : '**/*.lua';
	const normalizedSelectedSourceDirectory = normalizeSelectedSourceDirectory(formState.selectedSourceDirectory);
	const docsDirAbsolutePath = path.resolve(workspaceRoot, configuredDocsDir);
	const scopedCandidates = formState.selectionMode === 'folder' && normalizedSelectedSourceDirectory
		? configuredCandidates.filter((sourceAbsolutePath) => {
			const sourcePath = normalizePathForMarkdown(path.relative(workspaceRoot, sourceAbsolutePath));
			return getSourceDirectoryPath(sourcePath) === normalizedSelectedSourceDirectory;
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

	const previewLimit = Math.min(
		MAX_BATCH_UNIT_DOC_PREVIEW_LIMIT,
		Math.max(1, Number.isFinite(formState.maxFiles) ? Math.floor(formState.maxFiles) : DEFAULT_BATCH_UNIT_DOC_PREVIEW_LIMIT)
	);
	const limitedCandidates = afterMissingDocFilter.slice(0, previewLimit);

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

function buildBatchUnitDocReport(params: {
	timestamp: string;
	sourceGlob: string;
	missingDocsOnly: boolean;
	resolveOrphanedDocs: boolean;
	docsDir: string;
	totalCandidates: number;
	maxFiles: number;
	plannedActionCount: number;
	modelDetails?: DirectAnswerModelDetails;
	processedCount: number;
	generatedDocs: BatchGeneratedSummaryWrite[];
	movedDocs: Array<{ from: string; to: string }>;
	deletedDocs: string[];
	skippedActions: BatchSkippedAction[];
	failedActions: BatchFailedAction[];
	cancelled: boolean;
}): string {
	return [
		'# AI Dev Batch Summary Generation Report',
		'',
		`- Timestamp: ${params.timestamp}`,
		'- Execution mode: direct-experimental',
		`- Source glob: ${params.sourceGlob}`,
		`- Missing or empty summaries only: ${params.missingDocsOnly}`,
		`- Resolve orphaned docs: ${params.resolveOrphanedDocs}`,
		`- docsDir: ${params.docsDir}`,
		`- Total candidates: ${params.totalCandidates}`,
		`- Max files: ${params.maxFiles}`,
		`- Planned actions: ${params.plannedActionCount}`,
		`- Processed count: ${params.processedCount}`,
		`- Updated Summary Files: ${params.generatedDocs.length}`,
		`- Moved orphan docs: ${params.movedDocs.length}`,
		`- Deleted orphan docs: ${params.deletedDocs.length}`,
		`- Skipped actions: ${params.skippedActions.length}`,
		`- Failed actions: ${params.failedActions.length}`,
		`- Cancelled: ${params.cancelled}`,
		'- Selected model info:',
		`  - id: ${params.modelDetails?.id ?? 'n/a'}`,
		`  - name: ${params.modelDetails?.name ?? 'n/a'}`,
		`  - vendor: ${params.modelDetails?.vendor ?? 'n/a'}`,
		`  - family: ${params.modelDetails?.family ?? 'n/a'}`,
		'',
		'## Updated Summary Files',
		...(params.generatedDocs.length > 0
			? params.generatedDocs.map((item) => `- ${item.docPath}${item.sourcePaths.length > 0 ? ` | selected source files: ${item.sourcePaths.join(', ')}` : ''}`)
			: ['- none']),
		'',
		'## Moved Orphan Docs',
		...(params.movedDocs.length > 0 ? params.movedDocs.map((item) => `- ${item.from} -> ${item.to}`) : ['- none']),
		'',
		'## Deleted Orphan Docs',
		...(params.deletedDocs.length > 0 ? params.deletedDocs.map((filePath) => `- ${filePath}`) : ['- none']),
		'',
		'## Skipped Actions',
		...(params.skippedActions.length > 0
			? params.skippedActions.map((item) => {
				const sourceSegment = item.sourcePath
					? ` | source=${item.sourcePath}`
					: item.sourcePaths && item.sourcePaths.length > 0
						? ` | sources=${item.sourcePaths.join(', ')}`
						: '';
				return `- ${item.actionType} | doc=${item.docPath}${sourceSegment}: ${item.reason}`;
			})
			: ['- none']),
		'',
		'## Failed Actions',
		...(params.failedActions.length > 0
			? params.failedActions.map((item) => {
				const sourceSegment = item.sourcePath
					? ` | source=${item.sourcePath}`
					: item.sourcePaths && item.sourcePaths.length > 0
						? ` | sources=${item.sourcePaths.join(', ')}`
						: '';
				return `- ${item.actionType} | doc=${item.docPath}${sourceSegment}: ${item.error}`;
			})
			: ['- none']),
	].join('\n');
}

function normalizeRelativeDocPath(relativePath: string): string {
	return normalizePathForMarkdown(relativePath).replace(/^\.\//, '').replace(/\/+$/, '');
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

function buildNoDocumentationChangesRequiredFinding(params: {
	changedFilePaths: string[];
	gitDiffs: GitFileDiff[];
}): string {
	const commentOnlyDiff = params.gitDiffs.find((item) => isLikelyCommentOnlyDiff(item.diff));
	const fileForEvidence = commentOnlyDiff?.relativePath ?? params.changedFilePaths[0] ?? 'n/a';
	const diffEvidence = commentOnlyDiff
		? (extractFirstNonMetaDiffHunk(commentOnlyDiff.diff) ?? 'No diff hunk available.')
		: 'No comment-only diff detected from available diff samples.';

	return [
		'### No documentation changes required',
		'',
		'- Severity: info',
		'- Finding: Source change appears comment-only or otherwise non-semantic, so documentation regeneration is not required.',
		'- Recommended action: No documentation regeneration required.',
		'- Evidence:',
		`  - Changed source file: ${fileForEvidence}`,
		`  - Diff sample: ${diffEvidence}`,
	].join('\n');
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

function getErrorDetails(error: unknown): { message: string; stack: string } {
	const message = error instanceof Error ? error.message : String(error);
	const stack = error instanceof Error && typeof error.stack === 'string' ? error.stack : 'n/a';
	return { message, stack };
}

function stripSingleOuterCodeFence(text: string): { text: string; stripped: boolean } {
	const trimmed = text.trim();
	const fencedBlockMatch = trimmed.match(/^```[^\r\n]*\r?\n([\s\S]*?)\r?\n```$/);
	if (!fencedBlockMatch) {
		return { text, stripped: false };
	}

	return { text: fencedBlockMatch[1], stripped: true };
}

function getModelDetails(model: vscode.LanguageModelChat): {
	id: string;
	name: string;
	vendor: string;
	family: string;
} {
	const modelMetadata = model as unknown as {
		id?: unknown;
		name?: unknown;
		vendor?: unknown;
		family?: unknown;
	};

	return {
		id: typeof modelMetadata.id === 'string' ? modelMetadata.id : 'n/a',
		name: typeof modelMetadata.name === 'string' ? modelMetadata.name : 'n/a',
		vendor: typeof modelMetadata.vendor === 'string' ? modelMetadata.vendor : 'n/a',
		family: typeof modelMetadata.family === 'string' ? modelMetadata.family : 'n/a',
	};
}

type DirectAnswerModelDetails = ReturnType<typeof getModelDetails>;

type DirectModelCallProgressResult =
	| {
		status: 'completed';
		responseText: string;
		timestamp: string;
		modelDetails: DirectAnswerModelDetails;
	}
	| { status: 'cancelled' }
	| { status: 'handled-error' };

interface DirectModelCallOptions {
	title: string;
	diagnosticTitle: string;
	buildPrompt: (progress: vscode.Progress<{ message?: string }>, token: vscode.CancellationToken) => Promise<string>;
}

async function collectModelResponseText(
	model: vscode.LanguageModelChat,
	prompt: string,
	cancellationToken?: vscode.CancellationToken
): Promise<string> {
	const cancellation = cancellationToken ? undefined : new vscode.CancellationTokenSource();
	const token = cancellationToken ?? cancellation!.token;
	try {
		const response = await model.sendRequest(
			[
				vscode.LanguageModelChatMessage.User(prompt),
			],
			{},
			token
		);

		let responseText = '';
		for await (const chunk of response.text) {
			if (token.isCancellationRequested) {
				break;
			}

			responseText += chunk;
		}

		return responseText;
	} finally {
		cancellation?.dispose();
	}
}

async function runDirectModelCallWithProgress(options: DirectModelCallOptions): Promise<DirectModelCallProgressResult> {
	return vscode.window.withProgress<DirectModelCallProgressResult>(
		{
			location: vscode.ProgressLocation.Notification,
			title: options.title,
			cancellable: true,
		},
		async (progress, token) => {
			progress.report({ message: 'Reading workflow context' });
			const directPromptMarkdown = await options.buildPrompt(progress, token);
			if (token.isCancellationRequested) {
				return { status: 'cancelled' };
			}

			progress.report({ message: 'Selecting language model' });
			const models = await vscode.lm.selectChatModels();
			if (models.length === 0) {
				const timestamp = new Date().toISOString();
				const diagnosticMarkdown = [
					`# ${options.diagnosticTitle}`,
					'',
					`- Timestamp: ${timestamp}`,
					'- Error: No models were returned by vscode.lm.selectChatModels().',
					'',
					'## Direct request payload',
					'',
					'```markdown',
					directPromptMarkdown,
					'```',
				].join('\n');

				await vscode.window.showErrorMessage('No VS Code language model is available. Make sure a chat model is installed and enabled.');
				await openMarkdownDocument(diagnosticMarkdown);
				return { status: 'handled-error' };
			}

			if (token.isCancellationRequested) {
				return { status: 'cancelled' };
			}

			const [model] = models;
			const modelDetails = getModelDetails(model);

			progress.report({ message: 'Awaiting model response' });
			let responseText: string;
			try {
				responseText = await collectModelResponseText(model, directPromptMarkdown, token);
			} catch (error) {
				if (token.isCancellationRequested) {
					return { status: 'cancelled' };
				}

				throw error;
			}

			if (token.isCancellationRequested) {
				return { status: 'cancelled' };
			}

			return {
				status: 'completed',
				responseText,
				timestamp: new Date().toISOString(),
				modelDetails,
			};
		}
	);
}

async function openDirectResultMarkdown(params: {
	title: string;
	timestamp: string;
	modelDetails: DirectAnswerModelDetails;
	metadataLines?: string[];
	previewOnlyNote?: string;
	responseHeading?: string;
	responseText: string;
}): Promise<void> {
	const markdown = [
		`# ${params.title}`,
		'',
		`- Timestamp: ${params.timestamp}`,
		'- Selected model info:',
		`  - id: ${params.modelDetails.id}`,
		`  - name: ${params.modelDetails.name}`,
		`  - vendor: ${params.modelDetails.vendor}`,
		`  - family: ${params.modelDetails.family}`,
		...(params.metadataLines ?? []),
		...(params.previewOnlyNote
			? [
				'',
				'## Note',
				'',
				params.previewOnlyNote,
			]
			: []),
		'',
		`## ${params.responseHeading ?? 'Response'}`,
		'',
		'```markdown',
		params.responseText,
		'```',
	].join('\n');

	await openMarkdownDocument(markdown);
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

function resolveReviewFileTarget(
	uri?: vscode.Uri
): { activeFileUri: vscode.Uri; workspaceRoot: string } | { errorMessage: string } {
	if (uri && uri.scheme === 'file') {
		const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
		if (workspaceFolder) {
			return {
				activeFileUri: uri,
				workspaceRoot: workspaceFolder.uri.fsPath,
			};
		}

		return { errorMessage: 'Active file is not in an open workspace folder.' };
	}

	const activeContext = getActiveWorkspaceContext();
	if (activeContext) {
		return activeContext;
	}

	if (!vscode.window.activeTextEditor) {
		return { errorMessage: 'No active editor found. Open a source file and try again.' };
	}

	return { errorMessage: 'Active file is not in an open workspace folder.' };
}

async function resolveBatchSummaryFolderTarget(
	uri?: vscode.Uri
): Promise<{ workspaceRoot: string; selectedSourceDirectory: string } | { errorMessage: string }> {
	if (uri && uri.scheme === 'file') {
		const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
		if (!workspaceFolder) {
			return { errorMessage: 'Selection is not in an open workspace folder.' };
		}

		const workspaceRoot = workspaceFolder.uri.fsPath;
		let selectedDirectoryAbsolutePath = uri.fsPath;
		try {
			const stat = await fs.stat(uri.fsPath);
			if (!stat.isDirectory()) {
				selectedDirectoryAbsolutePath = path.dirname(uri.fsPath);
			}
		} catch {
			selectedDirectoryAbsolutePath = path.dirname(uri.fsPath);
		}

		return {
			workspaceRoot,
			selectedSourceDirectory: normalizeSelectedSourceDirectory(path.relative(workspaceRoot, selectedDirectoryAbsolutePath)) ?? '.',
		};
	}

	const activeContext = getActiveWorkspaceContext();
	if (activeContext) {
		return {
			workspaceRoot: activeContext.workspaceRoot,
			selectedSourceDirectory: normalizeSelectedSourceDirectory(path.relative(activeContext.workspaceRoot, path.dirname(activeContext.activeFileUri.fsPath))) ?? '.',
		};
	}

	return { errorMessage: 'No file or folder selection found. Select a source file or folder and try again.' };
}

async function openBatchSummaryGenerationWebview(params: {
	context: vscode.ExtensionContext;
	workspaceRoot: string;
	initialState: BatchUnitDocsFormState;
}): Promise<void> {
	const { context, workspaceRoot, initialState } = params;

	await openBatchUnitDocsWebview(context, {
		workspaceRoot,
		initialState,
		onPreview: async (formState: BatchUnitDocsFormState): Promise<BatchUnitDocPreviewResult> => {
			const aiDevConfig = await readAiDevConfig(workspaceRoot);
			const selection = await computeBatchUnitDocSelection({
				workspaceRoot,
				aiDevConfig,
				formState,
			});

			return {
				counts: selection.counts,
				items: selection.items,
			};
		},
		onGenerateRequested: async (formState: BatchUnitDocsFormState, previewPlan: BatchUnitDocPreviewResult) => {
			const aiDevConfig = await readAiDevConfig(workspaceRoot);
			const modeResolution = getExecutionModeFromConfig(aiDevConfig);
			if ('errorMessage' in modeResolution) {
				await vscode.window.showErrorMessage(modeResolution.errorMessage);
				return;
			}

			if (modeResolution.mode !== 'direct-experimental') {
				await vscode.window.showErrorMessage('Batch summary generation requires aiProvider.mode set to direct-experimental.');
				return;
			}

			const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
			const docsDirAbsolutePath = path.resolve(workspaceRoot, configuredDocsDir);
			const plannedActions = previewPlan.items.filter((action) => action.apply);
			if (plannedActions.length === 0) {
				await vscode.window.showInformationMessage('No selected rows to execute for the current preview.');
				return;
			}

			const moveActions = plannedActions.filter((action) => action.actionType === 'move-doc');
			const generateActions = plannedActions.filter((action) => action.actionType === 'generate-doc');
			const deleteActions = plannedActions.filter((action) => action.actionType === 'delete-doc');
			const groupedGenerateActions = new Map<string, BatchUnitDocPreviewItem[]>();
			for (const action of generateActions) {
				const existing = groupedGenerateActions.get(action.docPath);
				if (existing) {
					existing.push(action);
				} else {
					groupedGenerateActions.set(action.docPath, [action]);
				}
			}

			let groupedGeneratePromptContext:
				| { workflowFilePath: string; workflowFileContents: string; templateFilePath: string; templateFileContents: string }
				| undefined;
			if (groupedGenerateActions.size > 0) {
				if (!aiDevConfig.aiDevCorePathFromYaml) {
					await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
					return;
				}

				const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
				const workflowAbsolutePath = path.join(aiDevCorePath, 'workflows/generate-docs/generate-unit-doc.md');
				const templateAbsolutePath = path.join(aiDevCorePath, 'workflows/generate-docs/templates/unit-doc.md');
				const [workflowFileContents, templateFileContents] = await Promise.all([
					fs.readFile(workflowAbsolutePath, 'utf8'),
					fs.readFile(templateAbsolutePath, 'utf8'),
				]);
				groupedGeneratePromptContext = {
					workflowFilePath: formatRelativePath(workspaceRoot, workflowAbsolutePath),
					workflowFileContents,
					templateFilePath: formatRelativePath(workspaceRoot, templateAbsolutePath),
					templateFileContents,
				};
			}

			let model: vscode.LanguageModelChat | undefined;
			let modelDetails: DirectAnswerModelDetails | undefined;
			if (generateActions.length > 0) {
				const models = await vscode.lm.selectChatModels();
				if (models.length === 0) {
					await vscode.window.showErrorMessage('No VS Code language model is available. Make sure a chat model is installed and enabled.');
					return;
				}

				[model] = models;
				modelDetails = getModelDetails(model);
			}

			const generatedDocs: BatchGeneratedSummaryWrite[] = [];
			const movedDocs: Array<{ from: string; to: string }> = [];
			const deletedDocs: string[] = [];
			const touchedDocPaths = new Set<string>();
			const skippedActions: BatchSkippedAction[] = [];
			const failedActions: BatchFailedAction[] = [];
			let processedCount = 0;
			let cancelled = false;
			const totalActionCount = moveActions.length + groupedGenerateActions.size + deleteActions.length;
			let processedActionNumber = 0;

			await vscode.window.withProgress(
				{
					location: vscode.ProgressLocation.Notification,
					title: 'AI Dev: Executing batch summary action plan...',
					cancellable: true,
				},
				async (progress, token) => {
					for (const action of moveActions) {
						if (token.isCancellationRequested) {
							cancelled = true;
							break;
						}

						processedActionNumber += 1;
						progress.report({
							message: `${processedActionNumber}/${totalActionCount}: Move ${action.docPath}`,
						});

						try {
							if (!action.targetDocPath || !action.sourcePath) {
								skippedActions.push({
									actionType: 'move-doc',
									docPath: action.docPath,
									sourcePath: action.sourcePath,
									reason: 'invalid-move-plan',
								});
								processedCount += 1;
								continue;
							}

							const sourceDocAbsolutePath = path.resolve(workspaceRoot, action.docPath);
							const targetDocAbsolutePath = path.resolve(workspaceRoot, action.targetDocPath);
							if (!isPathInsideDirectory(sourceDocAbsolutePath, docsDirAbsolutePath) || !isPathInsideDirectory(targetDocAbsolutePath, docsDirAbsolutePath)) {
								skippedActions.push({
									actionType: 'move-doc',
									docPath: action.docPath,
									sourcePath: action.sourcePath,
									reason: 'blocked-outside-docsDir',
								});
								processedCount += 1;
								continue;
							}

							if (path.posix.basename(action.docPath).toLowerCase() === 'summary.md' || path.posix.basename(action.targetDocPath).toLowerCase() === 'summary.md') {
								skippedActions.push({
									actionType: 'move-doc',
									docPath: action.docPath,
									sourcePath: action.sourcePath,
									reason: 'summary-md-not-eligible',
								});
								processedCount += 1;
								continue;
							}

							let sourceStat: { isFile: () => boolean };
							try {
								sourceStat = await fs.stat(sourceDocAbsolutePath);
							} catch (error) {
								if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
									skippedActions.push({
										actionType: 'move-doc',
										docPath: action.docPath,
										sourcePath: action.sourcePath,
										reason: 'source-doc-missing',
									});
									processedCount += 1;
									continue;
								}

								throw error;
							}

							if (!sourceStat.isFile()) {
								skippedActions.push({
									actionType: 'move-doc',
									docPath: action.docPath,
									sourcePath: action.sourcePath,
									reason: 'source-not-file',
								});
								processedCount += 1;
								continue;
							}

							const existingDestinationContent = await readOptionalTextFile(targetDocAbsolutePath);
							if (existingDestinationContent !== undefined && existingDestinationContent.trim().length > 0) {
								skippedActions.push({
									actionType: 'move-doc',
									docPath: action.docPath,
									sourcePath: action.sourcePath,
									reason: 'destination-exists-non-empty',
								});
								processedCount += 1;
								continue;
							}

							if (existingDestinationContent !== undefined) {
								await fs.unlink(targetDocAbsolutePath);
							}

							await fs.mkdir(path.dirname(targetDocAbsolutePath), { recursive: true });
							try {
								await fs.rename(sourceDocAbsolutePath, targetDocAbsolutePath);
							} catch (error) {
								if ((error as NodeJS.ErrnoException).code === 'EXDEV') {
									await fs.copyFile(sourceDocAbsolutePath, targetDocAbsolutePath);
									await fs.unlink(sourceDocAbsolutePath);
								} else {
									throw error;
								}
							}

							movedDocs.push({ from: action.docPath, to: action.targetDocPath });
							touchedDocPaths.add(action.docPath);
							touchedDocPaths.add(action.targetDocPath);
							processedCount += 1;
						} catch (error) {
							if (token.isCancellationRequested) {
								cancelled = true;
								break;
							}

							const message = error instanceof Error ? error.message : String(error);
							failedActions.push({
								actionType: 'move-doc',
								docPath: action.docPath,
								sourcePath: action.sourcePath,
								error: message,
							});
							processedCount += 1;
						}
					}

					if (cancelled) {
						return;
					}

					for (const [docPath, groupedActions] of groupedGenerateActions) {
						if (token.isCancellationRequested) {
							cancelled = true;
							break;
						}

						const sourcePaths = groupedActions
							.map((item) => item.sourcePath)
							.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
							.map((item) => normalizePathForMarkdown(item));
						const uniqueSourcePaths = Array.from(new Set(sourcePaths)).sort((left, right) => left.localeCompare(right));

						processedActionNumber += 1;
						progress.report({
							message: `${processedActionNumber}/${totalActionCount}: Update ${docPath}`,
						});

						try {
							if (uniqueSourcePaths.length === 0) {
								skippedActions.push({
									actionType: 'generate-doc',
									docPath,
									sourcePaths: uniqueSourcePaths,
									reason: 'missing-source-path',
								});
								processedCount += 1;
								continue;
							}

							const expectedOutputAbsolutePath = path.resolve(workspaceRoot, docPath);
							if (!isPathInsideDirectory(expectedOutputAbsolutePath, docsDirAbsolutePath)) {
								skippedActions.push({
									actionType: 'generate-doc',
									docPath,
									sourcePaths: uniqueSourcePaths,
									reason: 'blocked-outside-docsDir',
								});
								processedCount += 1;
								continue;
							}

							if (!model) {
								skippedActions.push({
									actionType: 'generate-doc',
									docPath,
									sourcePaths: uniqueSourcePaths,
									reason: 'model-not-available',
								});
								processedCount += 1;
								continue;
							}

							if (!groupedGeneratePromptContext) {
								skippedActions.push({
									actionType: 'generate-doc',
									docPath,
									sourcePaths: uniqueSourcePaths,
									reason: 'missing-generate-context',
								});
								processedCount += 1;
								continue;
							}

							const existingSummaryContents = await readOptionalTextFile(expectedOutputAbsolutePath);
							const selectedSourceFiles: Array<{ path: string; contents: string }> = [];
							let hasInvalidSourceMapping = false;
							for (const sourcePath of uniqueSourcePaths) {
								const sourceAbsolutePath = path.resolve(workspaceRoot, sourcePath);
								const expectedSummaryPath = getExpectedDirectorySummaryPath({
									workspaceRoot,
									sourceFilePath: sourceAbsolutePath,
									docsDir: configuredDocsDir,
								});
								if (expectedSummaryPath !== docPath) {
									skippedActions.push({
										actionType: 'generate-doc',
										docPath,
										sourcePaths: uniqueSourcePaths,
										reason: `expected-path-mismatch:${sourcePath}->${expectedSummaryPath}`,
									});
									hasInvalidSourceMapping = true;
									break;
								}

								let sourceContents: string;
								try {
									sourceContents = await fs.readFile(sourceAbsolutePath, 'utf8');
								} catch (error) {
									if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
										skippedActions.push({
											actionType: 'generate-doc',
											docPath,
											sourcePaths: uniqueSourcePaths,
											reason: `source-missing:${sourcePath}`,
										});
										hasInvalidSourceMapping = true;
										break;
									}

									throw error;
								}

								selectedSourceFiles.push({
									path: sourcePath,
									contents: truncateText(sourceContents, MAX_DIRECT_FILE_CHARS),
								});
							}

							if (hasInvalidSourceMapping) {
								processedCount += 1;
								continue;
							}

							const directPromptMarkdown = buildGroupedGenerateUnitDocDirectPromptMarkdown({
								workspaceRoot,
								workflowFilePath: groupedGeneratePromptContext.workflowFilePath,
								workflowFileContents: groupedGeneratePromptContext.workflowFileContents,
								templateFilePath: groupedGeneratePromptContext.templateFilePath,
								templateFileContents: groupedGeneratePromptContext.templateFileContents,
								targetSummaryPath: docPath,
								existingSummaryContents: existingSummaryContents ? truncateText(existingSummaryContents, MAX_DIRECT_FILE_CHARS) : undefined,
								selectedSourceFiles,
							});
							const responseText = await collectModelResponseText(model, directPromptMarkdown, token);
							const cleanedResponse = stripSingleOuterCodeFence(responseText);
							if (token.isCancellationRequested) {
								cancelled = true;
								break;
							}

							if (cleanedResponse.text.trim().length === 0) {
								skippedActions.push({
									actionType: 'generate-doc',
									docPath,
									sourcePaths: uniqueSourcePaths,
									reason: 'empty-model-response',
								});
								processedCount += 1;
								continue;
							}

							await fs.mkdir(path.dirname(expectedOutputAbsolutePath), { recursive: true });
							await writeTextFileAtomicish(expectedOutputAbsolutePath, cleanedResponse.text);
							generatedDocs.push({ docPath, sourcePaths: uniqueSourcePaths });
							touchedDocPaths.add(docPath);
							processedCount += 1;
						} catch (error) {
							if (token.isCancellationRequested) {
								cancelled = true;
								break;
							}

							const message = error instanceof Error ? error.message : String(error);
							failedActions.push({
								actionType: 'generate-doc',
								docPath,
								sourcePaths: uniqueSourcePaths,
								error: message,
							});
							processedCount += 1;
						}
					}

					if (cancelled) {
						return;
					}

					for (const action of deleteActions) {
						if (token.isCancellationRequested) {
							cancelled = true;
							break;
						}

						processedActionNumber += 1;
						progress.report({
							message: `${processedActionNumber}/${totalActionCount}: Delete ${action.docPath}`,
						});

						try {
							const deleteAbsolutePath = path.resolve(workspaceRoot, action.docPath);
							if (!isPathInsideDirectory(deleteAbsolutePath, docsDirAbsolutePath)) {
								skippedActions.push({
									actionType: 'delete-doc',
									docPath: action.docPath,
									reason: 'blocked-outside-docsDir',
								});
								processedCount += 1;
								continue;
							}

							if (path.posix.basename(action.docPath).toLowerCase() === 'summary.md') {
								skippedActions.push({
									actionType: 'delete-doc',
									docPath: action.docPath,
									reason: 'summary-md-not-eligible',
								});
								processedCount += 1;
								continue;
							}

							let stat: { isFile: () => boolean };
							try {
								stat = await fs.stat(deleteAbsolutePath);
							} catch (error) {
								if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
									skippedActions.push({
										actionType: 'delete-doc',
										docPath: action.docPath,
										reason: 'doc-missing',
									});
									processedCount += 1;
									continue;
								}

								throw error;
							}

							if (!stat.isFile()) {
								skippedActions.push({
									actionType: 'delete-doc',
									docPath: action.docPath,
									reason: 'not-a-file',
								});
								processedCount += 1;
								continue;
							}

							await fs.unlink(deleteAbsolutePath);
							deletedDocs.push(action.docPath);
							touchedDocPaths.add(action.docPath);
							processedCount += 1;
						} catch (error) {
							if (token.isCancellationRequested) {
								cancelled = true;
								break;
							}

							const message = error instanceof Error ? error.message : String(error);
							failedActions.push({
								actionType: 'delete-doc',
								docPath: action.docPath,
								error: message,
							});
							processedCount += 1;
						}
					}
				}
			);

			const timestamp = new Date().toISOString();
			const reportMarkdown = buildBatchUnitDocReport({
				timestamp,
				sourceGlob: formState.sourceGlob.trim().length > 0 ? formState.sourceGlob.trim() : '**/*.lua',
				missingDocsOnly: formState.missingDocsOnly,
				resolveOrphanedDocs: formState.resolveOrphanedDocs,
				docsDir: configuredDocsDir,
				totalCandidates: previewPlan.counts.afterMissingDocFilter,
				maxFiles: formState.maxFiles,
				plannedActionCount: totalActionCount,
				modelDetails,
				processedCount,
				generatedDocs,
				movedDocs,
				deletedDocs,
				skippedActions,
				failedActions,
				cancelled,
			});

			await openMarkdownDocument(reportMarkdown);
			if (cancelled) {
				await vscode.window.showWarningMessage(
					`Batch action plan cancelled. Processed ${processedCount}/${totalActionCount}, generated ${generatedDocs.length}, moved ${movedDocs.length}, deleted ${deletedDocs.length}, skipped ${skippedActions.length}, failed ${failedActions.length}.`
				);
				return;
			}

			await vscode.window.showInformationMessage(
				`Batch action plan finished. Processed ${processedCount}/${totalActionCount}, generated ${generatedDocs.length}, moved ${movedDocs.length}, deleted ${deletedDocs.length}, skipped ${skippedActions.length}, failed ${failedActions.length}.`
			);
		},
	});
}

async function openArchitectureSummaryGenerationWebview(params: {
	context: vscode.ExtensionContext;
	workspaceRoot: string;
}): Promise<void> {
	const { context, workspaceRoot } = params;

	await openArchitectureSummaryWebview(context, {
		workspaceRoot,
		onPreview: async (): Promise<ArchitectureSummaryPreviewResult> => {
			const aiDevConfig = await readAiDevConfig(workspaceRoot);
			return discoverArchitectureSummaryPreview({
				workspaceRoot,
				aiDevConfig,
			});
		},
		onGenerateRequested: async (previewPlan: ArchitectureSummaryPreviewResult) => {
			const aiDevConfig = await readAiDevConfig(workspaceRoot);
			const modeResolution = getExecutionModeFromConfig(aiDevConfig);
			if ('errorMessage' in modeResolution) {
				await vscode.window.showErrorMessage(modeResolution.errorMessage);
				return;
			}

			const docsDir = getConfiguredDocsDir(aiDevConfig);
			const docsDirAbsolutePath = path.resolve(workspaceRoot, docsDir);
			const architectureSummaryPath = getArchitectureSummaryPath(aiDevConfig);
			const architectureSummaryAbsolutePath = path.resolve(workspaceRoot, architectureSummaryPath);
			const selection = selectArchitecturePreviewItems(previewPlan);
			if (selection.selectedItems.length === 0) {
				await vscode.window.showInformationMessage('No selected directories to include in architecture summary generation.');
				return;
			}

			const selectedStatusCounts = countArchitectureStatuses(selection.selectedItems);

			if (modeResolution.mode === 'prompt-only') {
				if (!aiDevConfig.aiDevCorePathFromYaml) {
					await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
					return;
				}

				const promptMarkdown = buildGenerateArchitectureSummaryPromptMarkdown({
					workspaceRoot,
					aiDevCorePath: aiDevConfig.aiDevCorePathFromYaml,
					docsDir,
					targetArchitecturePath: architectureSummaryPath,
					selectedDirectories: selection.selectedItems.map((item) => ({
						sourceDirectory: item.sourceDirectory,
						summaryPath: item.summaryPath,
						summaryStatus: item.status,
					})),
					omittedDirectories: selection.omittedItems.map((item) => ({
						sourceDirectory: item.sourceDirectory,
						summaryPath: item.summaryPath,
						summaryStatus: item.status,
					})),
				});

				await openMarkdownPromptAndCopy(
					promptMarkdown,
					'Generated architecture summary instructions and copied them to clipboard.'
				);
				return;
			}

			if (!aiDevConfig.aiDevCorePathFromYaml) {
				await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
				return;
			}

			const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
			const workflowAbsolutePath = path.join(aiDevCorePath, 'workflows/generate-docs/generate-architecture-summary.md');
			const templateAbsolutePath = path.join(aiDevCorePath, 'workflows/generate-docs/templates/architecture-summary.md');
			const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');

			const [workflowFileContents, templateFileContents, aiDevYamlContents, existingArchitectureSummaryContents] = await Promise.all([
				fs.readFile(workflowAbsolutePath, 'utf8'),
				fs.readFile(templateAbsolutePath, 'utf8'),
				fs.readFile(aiDevYamlPath, 'utf8'),
				readOptionalTextFile(architectureSummaryAbsolutePath),
			]);

			const selectedDirectoriesWithSummary = await Promise.all(selection.selectedItems.map(async (item) => {
				const summaryAbsolutePath = path.resolve(workspaceRoot, item.summaryPath);
				const summaryContents = item.status === 'missing'
					? undefined
					: await readOptionalTextFile(summaryAbsolutePath);

				return {
					sourceDirectory: item.sourceDirectory,
					summaryPath: item.summaryPath,
					summaryStatus: item.status,
					summaryContents: summaryContents !== undefined ? truncateText(summaryContents, MAX_DIRECT_FILE_CHARS) : undefined,
				};
			}));

			const directPromptMarkdown = buildGenerateArchitectureSummaryDirectPromptMarkdown({
				workspaceRoot,
				workflowFilePath: formatRelativePath(workspaceRoot, workflowAbsolutePath),
				workflowFileContents,
				templateFilePath: formatRelativePath(workspaceRoot, templateAbsolutePath),
				templateFileContents,
				aiDevYamlPath: '.ai-dev.yaml',
				aiDevYamlContents,
				docsDir,
				targetArchitecturePath: architectureSummaryPath,
				existingArchitectureSummaryContents: existingArchitectureSummaryContents
					? truncateText(existingArchitectureSummaryContents, MAX_DIRECT_FILE_CHARS)
					: undefined,
				selectedDirectories: selectedDirectoriesWithSummary,
				omittedDirectories: selection.omittedItems.map((item) => ({
					sourceDirectory: item.sourceDirectory,
					summaryPath: item.summaryPath,
					summaryStatus: item.status,
				})),
			});

			const progressResult = await runDirectModelCallWithProgress({
				title: 'AI Dev: Generating architecture summary...',
				diagnosticTitle: 'AI Dev Generate Architecture Summary Direct Diagnostic',
				buildPrompt: async () => directPromptMarkdown,
			});

			if (progressResult.status === 'cancelled') {
				await vscode.window.showInformationMessage('AI Dev generate architecture summary direct mode cancelled.');
				return;
			}

			if (progressResult.status === 'handled-error') {
				return;
			}

			const cleanedResponse = stripSingleOuterCodeFence(progressResult.responseText);
			const responseText = cleanedResponse.text;
			const skipped: string[] = [];
			const failed: string[] = [];
			let writeStatus = 'preview-only';
			let fileWritten = false;
			let writtenFilePath: string | undefined;

			if (responseText.trim().length === 0) {
				skipped.push('empty-model-response');
				const report = buildArchitectureSummaryReport({
					timestamp: progressResult.timestamp,
					targetPath: architectureSummaryPath,
					docsDir,
					modelDetails: progressResult.modelDetails,
					selectedCount: selection.selectedItems.length,
					existingCount: selectedStatusCounts.existingCount,
					missingCount: selectedStatusCounts.missingCount,
					emptyCount: selectedStatusCounts.emptyCount,
					responseLength: responseText.length,
					fenceStripped: cleanedResponse.stripped,
					writeStatus: 'skipped-empty-response',
					fileWritten,
					writtenFilePath,
					skipped,
					failed,
				});

				await openMarkdownDocument(report);
				await vscode.window.showWarningMessage('The selected language model returned an empty response. Wait a moment and retry.');
				return;
			}

			if (!isPathInsideDirectory(architectureSummaryAbsolutePath, docsDirAbsolutePath)) {
				writeStatus = 'blocked-outside-docsDir';
				skipped.push('blocked-outside-docsDir');
				await vscode.window.showErrorMessage(
					`Direct writes are restricted to documentation.docsDir (${docsDir}). Target path ${architectureSummaryPath} is outside that directory.`
				);
			} else {
				let shouldWrite = allowDocsDirWritesForSession;
				if (!allowDocsDirWritesForSession) {
					const writeChoice = await vscode.window.showInformationMessage(
						`Write generated architecture summary to ${architectureSummaryPath}?`,
						{ modal: true },
						'Write Once',
						'Allow docsDir writes this session',
						'Preview Only'
					);

					if (writeChoice === 'Write Once') {
						shouldWrite = true;
					} else if (writeChoice === 'Allow docsDir writes this session') {
						allowDocsDirWritesForSession = true;
						shouldWrite = true;
					} else {
						shouldWrite = false;
					}
				}

				if (shouldWrite) {
					try {
						await fs.mkdir(path.dirname(architectureSummaryAbsolutePath), { recursive: true });
						await writeTextFileAtomicish(architectureSummaryAbsolutePath, responseText);
						writeStatus = 'written';
						fileWritten = true;
						writtenFilePath = architectureSummaryPath;
					} catch (error) {
						writeStatus = 'write-failed';
						failed.push(error instanceof Error ? error.message : String(error));
					}
				}
			}

			const report = buildArchitectureSummaryReport({
				timestamp: progressResult.timestamp,
				targetPath: architectureSummaryPath,
				docsDir,
				modelDetails: progressResult.modelDetails,
				selectedCount: selection.selectedItems.length,
				existingCount: selectedStatusCounts.existingCount,
				missingCount: selectedStatusCounts.missingCount,
				emptyCount: selectedStatusCounts.emptyCount,
				responseLength: responseText.length,
				fenceStripped: cleanedResponse.stripped,
				writeStatus,
				fileWritten,
				writtenFilePath,
				skipped,
				failed,
			});

			await openMarkdownDocument(report);
			if (fileWritten) {
				await vscode.window.showInformationMessage(`AI Dev wrote generated architecture summary to ${architectureSummaryPath}.`);
			} else {
				await vscode.window.showInformationMessage('AI Dev architecture summary generation completed in preview-only mode.');
			}
		},
	});
}

async function buildReviewFileDocumentationPromptBundle(target: {
	activeFileUri: vscode.Uri;
	workspaceRoot: string;
}): Promise<{ promptMarkdown: string }> {
	const { activeFileUri, workspaceRoot } = target;
	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	if (!aiDevConfig.aiDevCorePathFromYaml) {
		throw new Error('Missing aiDevCore.path in .ai-dev.yaml.');
	}

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const selectedSourcePath = getSelectedSourcePath(workspaceRoot, activeFileUri.fsPath);
	const expectedSummaryPath = getExpectedDirectorySummaryPath({
		workspaceRoot,
		sourceFilePath: activeFileUri.fsPath,
		docsDir: configuredDocsDir,
	});
	const summaryFile = getRootSummaryFilePath(aiDevConfig);

	return {
		promptMarkdown: buildFileDocumentationReviewPromptMarkdown({
			aiDevCorePath: aiDevConfig.aiDevCorePathFromYaml,
			workspaceRoot,
			selectedSourcePath,
			targetSummaryPath: expectedSummaryPath,
			summaryFile,
		}),
	};
}

async function buildReviewDocumentationPromptBundle(workspaceRoot: string): Promise<{
	promptMarkdown: string;
}> {
	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	if (!aiDevConfig.aiDevCorePathFromYaml) {
		throw new Error('Missing aiDevCore.path in .ai-dev.yaml.');
	}

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

	const deterministicFindings = await collectDeterministicDocumentationFindings({
		workspaceRoot,
		aiDevConfig,
		changedFilePaths,
		renameRecords,
	});
	const deterministicFindingsMarkdown = toDeterministicFindingsMarkdown(deterministicFindings);

	return {
		promptMarkdown: buildReviewDocumentationPromptMarkdown({
			aiDevCorePath: aiDevConfig.aiDevCorePathFromYaml,
			workspaceRoot,
			changedFilePaths: changedFilePaths.map((filePath) => normalizePathForMarkdown(filePath)),
			deterministicFindingsMarkdown,
		}),
	};
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
	if (!aiDevConfig.aiDevCorePathFromYaml) {
		throw new Error('Missing aiDevCore.path in .ai-dev.yaml.');
	}

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const selectedSourcePath = getSelectedSourcePath(workspaceRoot, activeFileUri.fsPath);
	const expectedSummaryPath = getExpectedDirectorySummaryPath({
		workspaceRoot,
		sourceFilePath: activeFileUri.fsPath,
		docsDir: configuredDocsDir,
	});
	const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
	const workflowFilePath = path.join(aiDevCorePath, 'workflows/generate-docs/generate-unit-doc.md');
	const templateFilePath = path.join(aiDevCorePath, 'workflows/generate-docs/templates/unit-doc.md');
	const sourceFilePath = activeFileUri.fsPath;
	const expectedSummaryAbsolutePath = path.resolve(workspaceRoot, expectedSummaryPath);
	const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');

	const [workflowFileContents, templateFileContents, sourceFileContents, expectedSummaryContents, aiDevYamlContents] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		fs.readFile(templateFilePath, 'utf8'),
		fs.readFile(sourceFilePath, 'utf8'),
		readOptionalTextFile(expectedSummaryAbsolutePath),
		fs.readFile(aiDevYamlPath, 'utf8'),
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
		'.ai-dev.yaml',
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
	if (!aiDevConfig.aiDevCorePathFromYaml) {
		throw new Error('Missing aiDevCore.path in .ai-dev.yaml.');
	}

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

	const deterministicFindings = await collectDeterministicDocumentationFindings({
		workspaceRoot,
		aiDevConfig,
		changedFilePaths,
		renameRecords,
	});
	const deterministicFindingsMarkdown = toDeterministicFindingsMarkdown(deterministicFindings);

	const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
	const workflowFilePath = path.join(aiDevCorePath, 'workflows/review/review-documentation.md');
	const findingTemplatePath = path.join(aiDevCorePath, 'workflows/review/finding-template.md');
	const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');

	const [workflowFileContents, findingTemplateContents, aiDevYamlContents, changedFilesWithContent, gitDiffs] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		fs.readFile(findingTemplatePath, 'utf8'),
		fs.readFile(aiDevYamlPath, 'utf8'),
		existingChangedFilesWithContent(workspaceRoot, changedFilePaths),
		getGitDiffForFiles(workspaceRoot, changedFilePaths),
	]);

	const boundedChangedFilesWithContent = changedFilesWithContent
		.slice(0, MAX_DIRECT_CHANGED_FILE_CONTENTS)
		.map((file) => ({
			relativePath: file.relativePath,
			contents: truncateText(file.contents, MAX_DIRECT_FILE_CHARS),
		}));

	const normalizedChangedFilePaths = changedFilePaths.map((filePath) => normalizePathForMarkdown(filePath));
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
		'.ai-dev.yaml',
		'',
		'```yaml',
		aiDevYamlContents,
		'```',
		'',
		'Changed file paths:',
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

async function buildReviewFileDocumentationDirectPromptBundle(target: {
	activeFileUri: vscode.Uri;
	workspaceRoot: string;
}): Promise<{
	directPromptMarkdown: string;
	selectedSourcePath: string;
	expectedSummaryPath: string;
}> {
	const { activeFileUri, workspaceRoot } = target;
	const aiDevConfig = await readAiDevConfig(workspaceRoot);
	if (!aiDevConfig.aiDevCorePathFromYaml) {
		throw new Error('Missing aiDevCore.path in .ai-dev.yaml.');
	}

	const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
	const selectedSourcePath = getSelectedSourcePath(workspaceRoot, activeFileUri.fsPath);
	const expectedSummaryPath = getExpectedDirectorySummaryPath({
		workspaceRoot,
		sourceFilePath: activeFileUri.fsPath,
		docsDir: configuredDocsDir,
	});
	const summaryFile = getRootSummaryFilePath(aiDevConfig);
	const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
	const workflowFilePath = path.join(aiDevCorePath, 'workflows/review/review-documentation.md');
	const findingTemplatePath = path.join(aiDevCorePath, 'workflows/review/finding-template.md');
	const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');
	const sourceFilePath = activeFileUri.fsPath;
	const expectedSummaryAbsolutePath = path.resolve(workspaceRoot, expectedSummaryPath);
	const summaryFileAbsolutePath = path.resolve(workspaceRoot, summaryFile);

	const [workflowFileContents, findingTemplateContents, sourceFileContents, expectedSummaryContents, summaryFileContents, aiDevYamlContents] = await Promise.all([
		fs.readFile(workflowFilePath, 'utf8'),
		fs.readFile(findingTemplatePath, 'utf8'),
		fs.readFile(sourceFilePath, 'utf8'),
		readOptionalTextFile(expectedSummaryAbsolutePath),
		readOptionalTextFile(summaryFileAbsolutePath),
		fs.readFile(aiDevYamlPath, 'utf8'),
	]);

	const directPromptMarkdown = [
		'AI Dev direct task: review-file-docs',
		'',
		`Workspace: ${normalizePathForMarkdown(workspaceRoot)}`,
		`Source file path: ${selectedSourcePath}`,
		`Target summary file path: ${expectedSummaryPath}`,
		`Summary file path: ${summaryFile}`,
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
		'.ai-dev.yaml',
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
		expectedSummaryContents ? 'Target summary file contents:' : 'Target summary file contents: not found',
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
		summaryFileContents ? 'Summary file contents:' : 'Summary file contents: not found',
		summaryFileContents ? summaryFile : '',
		...(summaryFileContents
			? [
				'',
				'```markdown',
				summaryFileContents,
				'```',
			]
			: []),
		'',
		'Instructions:',
		'Review source against the target summary file and summary routing artifacts for mismatches, stale docs, and missing routed documentation.',
		'Return findings in markdown using the provided finding template style.',
	].join('\n');

	return {
		directPromptMarkdown,
		selectedSourcePath,
		expectedSummaryPath,
	};
}

export function activate(context: vscode.ExtensionContext) {
	setAiDevExtensionRootPath(context.extensionPath);
	registerAiDevActionsView(context);
	const workflowDetailsProvider = new WorkflowDetailsViewProvider();
	context.subscriptions.push(
		vscode.window.registerWebviewViewProvider('aiDev.workflowDetails', workflowDetailsProvider)
	);

	const selectWorkflowCommand = vscode.commands.registerCommand(
		SELECT_WORKFLOW_COMMAND,
		async (workflowId: string) => {
			await workflowDetailsProvider.setActiveWorkflow(workflowId);
			try {
				await vscode.commands.executeCommand('aiDev.workflowDetails.focus');
			} catch {
				// If focus command is unavailable, keep the selection change only.
			}
		}
	);

	const generateUnitDocCommand = vscode.commands.registerCommand(
		GENERATE_UNIT_DOC_COMMAND,
		async (uri?: vscode.Uri) => {
			let activeFileUri: vscode.Uri | undefined;
			let workspaceRoot: string | undefined;

			if (uri && uri.scheme === 'file') {
				const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
				if (workspaceFolder) {
					activeFileUri = uri;
					workspaceRoot = workspaceFolder.uri.fsPath;
				}
			}

			if (!activeFileUri || !workspaceRoot) {
				const activeContext = getActiveWorkspaceContext();
				if (!activeContext) {
					if (!vscode.window.activeTextEditor) {
						await vscode.window.showErrorMessage('No active editor found. Open a source file and try again.');
						return;
					}

					await vscode.window.showErrorMessage('Active file is not in an open workspace folder.');
					return;
				}

				activeFileUri = activeContext.activeFileUri;
				workspaceRoot = activeContext.workspaceRoot;
			}

			try {
				const aiDevConfig = await readAiDevConfig(workspaceRoot);
				if (!aiDevConfig.aiDevCorePathFromYaml) {
					await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
					return;
				}

				const modeResolution = getExecutionModeFromConfig(aiDevConfig);
				if ('errorMessage' in modeResolution) {
					await vscode.window.showErrorMessage(modeResolution.errorMessage);
					return;
				}

				if (modeResolution.mode === 'direct-experimental') {
					try {
						const bundle = await buildGenerateUnitDocDirectPromptBundle({
							workspaceRoot,
							activeFileUri,
							aiDevConfig,
						});
						const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
						const docsDirAbsolutePath = path.resolve(workspaceRoot, configuredDocsDir);
						const expectedOutputAbsolutePath = path.resolve(workspaceRoot, bundle.expectedSummaryPath);
						const progressResult = await runDirectModelCallWithProgress({
							title: 'AI Dev: Generating summary doc...',
							diagnosticTitle: 'AI Dev Generate Summary Doc Direct Diagnostic',
							buildPrompt: async () => bundle.directPromptMarkdown,
						});

						if (progressResult.status === 'cancelled') {
							await vscode.window.showInformationMessage('AI Dev generate summary doc direct mode cancelled.');
							return;
						}

						if (progressResult.status === 'handled-error') {
							return;
						}

						const cleanedResponse = stripSingleOuterCodeFence(progressResult.responseText);
						const responseText = cleanedResponse.text;
						const responseLength = responseText.length;
						if (responseText.trim().length === 0) {
							await openDirectResultMarkdown({
								title: 'AI Dev Generate Summary Doc Direct Result',
								timestamp: progressResult.timestamp,
								modelDetails: progressResult.modelDetails,
								metadataLines: [
									'',
									`- Source file path: ${bundle.selectedSourcePath}`,
									`- Output file path: ${bundle.expectedSummaryPath}`,
									`- docsDir: ${configuredDocsDir}`,
									'- Decision: Blocked: empty model response',
									'- Write status: skipped-empty-response',
									'- File written: false',
									`- Response fence stripped: ${cleanedResponse.stripped}`,
									`- Response length: ${responseLength}`,
								],
								previewOnlyNote: 'The selected language model returned an empty response. This can happen if Copilot/model services are still loading. Wait a moment and retry. No directories were created and no file was written.',
								responseHeading: 'Generated Documentation',
								responseText,
							});
							await vscode.window.showWarningMessage('The selected language model returned an empty response. This can happen if Copilot/model services are still loading. Wait a moment and retry.');
							return;
						}

						let writeStatus = 'preview-only';
						let wroteFile = false;
						let writeDecision = 'Preview Only';

						if (!isPathInsideDirectory(expectedOutputAbsolutePath, docsDirAbsolutePath)) {
							writeStatus = 'blocked-outside-docsDir';
							writeDecision = 'Blocked: expected output path is outside docsDir';
							await vscode.window.showErrorMessage(
								`Direct writes are restricted to documentation.docsDir (${configuredDocsDir}). Expected path ${bundle.expectedSummaryPath} is outside that directory.`
							);
						} else {
							let shouldWrite = allowDocsDirWritesForSession;
							if (!allowDocsDirWritesForSession) {
								const writeChoice = await vscode.window.showInformationMessage(
									`Write generated documentation to ${bundle.expectedSummaryPath}?`,
									{ modal: true },
									'Write Once',
									'Allow docsDir writes this session',
									'Preview Only'
								);

								if (writeChoice === 'Write Once') {
									shouldWrite = true;
									writeDecision = 'Write Once';
								} else if (writeChoice === 'Allow docsDir writes this session') {
									allowDocsDirWritesForSession = true;
									shouldWrite = true;
									writeDecision = 'Allow docsDir writes this session';
								} else {
									shouldWrite = false;
									writeDecision = 'Preview Only';
								}
							} else {
								writeDecision = 'Allow docsDir writes this session (already granted)';
							}

							if (shouldWrite) {
								await fs.mkdir(path.dirname(expectedOutputAbsolutePath), { recursive: true });
								await writeTextFileAtomicish(expectedOutputAbsolutePath, responseText);
								writeStatus = 'written';
								wroteFile = true;
								await vscode.window.showInformationMessage(`AI Dev wrote generated documentation to ${bundle.expectedSummaryPath}.`);
							}
						}

						await openDirectResultMarkdown({
							title: 'AI Dev Generate Summary Doc Direct Result',
							timestamp: progressResult.timestamp,
							modelDetails: progressResult.modelDetails,
							metadataLines: [
								'',
								`- Source file path: ${bundle.selectedSourcePath}`,
								`- Output file path: ${bundle.expectedSummaryPath}`,
								`- docsDir: ${configuredDocsDir}`,
								`- Decision: ${writeDecision}`,
								`- Write status: ${writeStatus}`,
								`- File written: ${wroteFile}`,
								`- Response fence stripped: ${cleanedResponse.stripped}`,
								`- Response length: ${responseLength}`,
							],
							previewOnlyNote: wroteFile
								? `Generated documentation was written to ${bundle.expectedSummaryPath}.`
								: 'Generated documentation was previewed only. No file was written.',
							responseHeading: 'Generated Documentation',
							responseText,
						});
						await vscode.window.showInformationMessage(`AI Dev direct summary doc generation completed. Response length: ${responseLength}.`);
					} catch (error) {
						const { message, stack } = getErrorDetails(error);
						const timestamp = new Date().toISOString();
						const diagnosticMarkdown = [
							'# AI Dev Generate Summary Doc Direct Diagnostic',
							'',
							`- Timestamp: ${timestamp}`,
							`- Error message: ${message}`,
							'',
							'## Error stack',
							'',
							'```text',
							stack,
							'```',
						].join('\n');

						await openMarkdownDocument(diagnosticMarkdown);
						await vscode.window.showErrorMessage(`AI Dev generate summary doc direct mode failed: ${message}`);
					}

					return;
				}

				const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);

				const selectedSourcePath = getSelectedSourcePath(workspaceRoot, activeFileUri.fsPath);
				const expectedSummaryPath = getExpectedDirectorySummaryPath({
					workspaceRoot,
					sourceFilePath: activeFileUri.fsPath,
					docsDir: configuredDocsDir,
				});

				const promptMarkdown = buildUnitDocPromptMarkdown({
					workspaceRoot,
					aiDevCorePath: aiDevConfig.aiDevCorePathFromYaml,
					selectedSourcePath,
					targetSummaryPath: expectedSummaryPath,
				});
				await openMarkdownPromptAndCopy(promptMarkdown, 'Generated summary instructions and copied them to clipboard.');
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				await vscode.window.showErrorMessage(`Failed to generate summary: ${message}`);
			}
		}
	);

	const generateUnitDocsBatchExperimentalCommand = vscode.commands.registerCommand(
		GENERATE_UNIT_DOCS_BATCH_EXPERIMENTAL_COMMAND,
		async () => {
			const workspaceRoot = getOpenWorkspaceRoot();
			if (!workspaceRoot) {
				await vscode.window.showErrorMessage('No workspace is open.');
				return;
			}

			try {
				await openBatchSummaryGenerationWebview({
					context,
					workspaceRoot,
					initialState: {
						sourceGlob: '**/*.lua',
						missingDocsOnly: true,
						resolveOrphanedDocs: false,
						maxFiles: DEFAULT_BATCH_UNIT_DOC_PREVIEW_LIMIT,
						selectionMode: 'workspace',
					},
				});
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				await vscode.window.showErrorMessage(`Failed to run batch summary generation: ${message}`);
			}
		}
	);

	const generateArchitectureSummaryCommand = vscode.commands.registerCommand(
		GENERATE_ARCHITECTURE_SUMMARY_COMMAND,
		async () => {
			const workspaceRoot = getOpenWorkspaceRoot();
			if (!workspaceRoot) {
				await vscode.window.showErrorMessage('No workspace is open.');
				return;
			}

			try {
				await openArchitectureSummaryGenerationWebview({
					context,
					workspaceRoot,
				});
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				await vscode.window.showErrorMessage(`Failed to open architecture summary generation: ${message}`);
			}
		}
	);

	const updateFolderSummaryCommand = vscode.commands.registerCommand(
		UPDATE_FOLDER_SUMMARY_COMMAND,
		async (uri?: vscode.Uri) => {
			const target = await resolveBatchSummaryFolderTarget(uri);
			if ('errorMessage' in target) {
				await vscode.window.showErrorMessage(target.errorMessage);
				return;
			}

			try {
				const aiDevConfig = await readAiDevConfig(target.workspaceRoot);
				const configuredDocsDir = getConfiguredDocsDir(aiDevConfig);
				const selectedSummaryFile = getSelectedDirectorySummaryFile(target.selectedSourceDirectory, configuredDocsDir);

				await openBatchSummaryGenerationWebview({
					context,
					workspaceRoot: target.workspaceRoot,
					initialState: {
						sourceGlob: '**/*.lua',
						missingDocsOnly: true,
						resolveOrphanedDocs: false,
						maxFiles: DEFAULT_BATCH_UNIT_DOC_PREVIEW_LIMIT,
						selectionMode: 'folder',
						selectedSourceDirectory: target.selectedSourceDirectory,
						selectedSummaryFile,
					},
				});
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				await vscode.window.showErrorMessage(`Failed to open folder summary generation: ${message}`);
			}
		}
	);

	const reviewDocumentationCommand = vscode.commands.registerCommand(
		REVIEW_DOCUMENTATION_COMMAND,
		async () => {
			const workspaceRoot = getOpenWorkspaceRoot();
			if (!workspaceRoot) {
				await vscode.window.showErrorMessage('No workspace is open.');
				return;
			}

			try {
				const aiDevConfig = await readAiDevConfig(workspaceRoot);
				if (!aiDevConfig.aiDevCorePathFromYaml) {
					await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
					return;
				}

				const modeResolution = getExecutionModeFromConfig(aiDevConfig);
				if ('errorMessage' in modeResolution) {
					await vscode.window.showErrorMessage(modeResolution.errorMessage);
					return;
				}

				if (modeResolution.mode === 'direct-experimental') {
					try {
						const bundle = await buildReviewDocumentationDirectPromptBundle(workspaceRoot, aiDevConfig);
						const progressResult = await runDirectModelCallWithProgress({
							title: 'AI Dev: Reviewing changed docs...',
							diagnosticTitle: 'AI Dev Review Changed Docs Direct Diagnostic',
							buildPrompt: async () => bundle.directPromptMarkdown,
						});

						if (progressResult.status === 'cancelled') {
							await vscode.window.showInformationMessage('AI Dev review changed docs direct mode cancelled.');
							return;
						}

						if (progressResult.status === 'handled-error') {
							return;
						}

						const deterministicFindingsMarkdown = toDeterministicFindingsMarkdown(bundle.deterministicFindings);
						const trimmedModelResponse = progressResult.responseText.trim();
						const usedModelFallback = trimmedModelResponse.length === 0;
						const modelReviewFindings = usedModelFallback
							? [
								'No model findings returned.',
								'',
								buildNoDocumentationChangesRequiredFinding({
									changedFilePaths: bundle.changedFilePaths,
									gitDiffs: bundle.gitDiffs,
								}),
							].join('\n')
							: progressResult.responseText;
						const fallbackDebugPromptMarkdown = usedModelFallback
							? [
								'',
								'## Debug: Direct prompt sent to model',
								'',
								'```markdown',
								bundle.directPromptMarkdown,
								'```',
							].join('\n')
							: '';
						const combinedReviewFindings = [
							deterministicFindingsMarkdown,
							'',
							'## Model Review Findings',
							'',
							modelReviewFindings,
							fallbackDebugPromptMarkdown,
						].join('\n');

						await openDirectResultMarkdown({
							title: 'AI Dev Review Changed Docs Direct Result',
							timestamp: progressResult.timestamp,
							modelDetails: progressResult.modelDetails,
							metadataLines: [
								'',
								`- Changed files considered: ${bundle.changedFilePaths.length}`,
								`- Deterministic findings: ${bundle.deterministicFindings.length}`,
								`- Result status: ${usedModelFallback ? 'fallback-no-model-findings' : 'model-findings-returned'}`,
								...(usedModelFallback ? ['- Warning: No model findings returned.'] : []),
							],
							responseHeading: 'Review Findings',
							responseText: combinedReviewFindings,
						});
						if (usedModelFallback) {
							await vscode.window.showWarningMessage('No model findings were returned. The selected language model may have returned an empty response while Copilot/model services were still loading. Wait a moment and retry. Displaying fallback no-action review finding.');
						}
						await vscode.window.showInformationMessage(`AI Dev direct changed-docs review completed. Response length: ${progressResult.responseText.length}.`);
					} catch (error) {
						const { message, stack } = getErrorDetails(error);
						const timestamp = new Date().toISOString();
						const diagnosticMarkdown = [
							'# AI Dev Review Changed Docs Direct Diagnostic',
							'',
							`- Timestamp: ${timestamp}`,
							`- Error message: ${message}`,
							'',
							'## Error stack',
							'',
							'```text',
							stack,
							'```',
						].join('\n');

						await openMarkdownDocument(diagnosticMarkdown);
						await vscode.window.showErrorMessage(`AI Dev review changed docs direct mode failed: ${message}`);
					}

					return;
				}

				const { promptMarkdown } = await buildReviewDocumentationPromptBundle(workspaceRoot);
				await openMarkdownPromptAndCopy(promptMarkdown, 'Generated changed-docs review instructions and copied them to clipboard.');
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				if (
					message === 'Missing aiDevCore.path in .ai-dev.yaml.' ||
					message === 'No changed files found.' ||
					message.startsWith('Failed to read git changed files:')
				) {
					await vscode.window.showErrorMessage(message);
					return;
				}

				await vscode.window.showErrorMessage(`Failed to generate documentation review: ${message}`);
			}
		}
	);

	const reviewFileDocumentationCommand = vscode.commands.registerCommand(
		REVIEW_FILE_DOCUMENTATION_COMMAND,
		async (uri?: vscode.Uri) => {
			const target = resolveReviewFileTarget(uri);
			if ('errorMessage' in target) {
				await vscode.window.showErrorMessage(target.errorMessage);
				return;
			}

			try {
				const aiDevConfig = await readAiDevConfig(target.workspaceRoot);
				if (!aiDevConfig.aiDevCorePathFromYaml) {
					await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
					return;
				}

				const docsDir = getConfiguredDocsDir(aiDevConfig);
				const docsDirAbsolutePath = path.resolve(target.workspaceRoot, docsDir);
				if (isPathInsideDirectory(target.activeFileUri.fsPath, docsDirAbsolutePath)) {
					await vscode.window.showWarningMessage(
						'Review File Docs expects a source file, but the selected file appears to be generated AI documentation. Select the matching source file instead.'
						+ ` Selected docs directory: ${docsDir}.`
					);
					return;
				}

				const modeResolution = getExecutionModeFromConfig(aiDevConfig);
				if ('errorMessage' in modeResolution) {
					await vscode.window.showErrorMessage(modeResolution.errorMessage);
					return;
				}

				if (modeResolution.mode === 'direct-experimental') {
					try {
						const bundle = await buildReviewFileDocumentationDirectPromptBundle(target);
						const progressResult = await runDirectModelCallWithProgress({
							title: 'AI Dev: Reviewing file docs...',
							diagnosticTitle: 'AI Dev Review File Docs Direct Diagnostic',
							buildPrompt: async () => bundle.directPromptMarkdown,
						});

						if (progressResult.status === 'cancelled') {
							await vscode.window.showInformationMessage('AI Dev review file docs direct mode cancelled.');
							return;
						}

						if (progressResult.status === 'handled-error') {
							return;
						}

						if (progressResult.responseText.trim().length === 0) {
							await vscode.window.showWarningMessage('The selected language model returned an empty response. This can happen if Copilot/model services are still loading. Wait a moment and retry.');
						}

						await openDirectResultMarkdown({
							title: 'AI Dev Review File Docs Direct Result',
							timestamp: progressResult.timestamp,
							modelDetails: progressResult.modelDetails,
							metadataLines: [
								'',
								`- Source file path: ${bundle.selectedSourcePath}`,
								`- Target summary file path: ${bundle.expectedSummaryPath}`,
							],
							responseHeading: 'Review Findings',
							responseText: progressResult.responseText,
						});
						await vscode.window.showInformationMessage(`AI Dev direct file-docs review completed. Response length: ${progressResult.responseText.length}.`);
					} catch (error) {
						const { message, stack } = getErrorDetails(error);
						const timestamp = new Date().toISOString();
						const diagnosticMarkdown = [
							'# AI Dev Review File Docs Direct Diagnostic',
							'',
							`- Timestamp: ${timestamp}`,
							`- Error message: ${message}`,
							'',
							'## Error stack',
							'',
							'```text',
							stack,
							'```',
						].join('\n');

						await openMarkdownDocument(diagnosticMarkdown);
						await vscode.window.showErrorMessage(`AI Dev review file docs direct mode failed: ${message}`);
					}

					return;
				}

				const { promptMarkdown } = await buildReviewFileDocumentationPromptBundle(target);
				await openMarkdownPromptAndCopy(promptMarkdown, 'Generated file-docs review instructions and copied them to clipboard.');
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
					return;
				}

				const message = error instanceof Error ? error.message : String(error);
				if (
					message === 'Missing aiDevCore.path in .ai-dev.yaml.' ||
					message === 'Unsafe documentation summary path resolved.'
				) {
					await vscode.window.showErrorMessage(message);
					return;
				}

				await vscode.window.showErrorMessage(`Failed to generate file documentation review: ${message}`);
			}
		}
	);

	const answerFromAiDocsCommand = vscode.commands.registerCommand(
		ANSWER_FROM_AI_DOCS_COMMAND,
		async () => {
			const workspaceRoot = getOpenWorkspaceRoot();
			if (!workspaceRoot) {
				await vscode.window.showErrorMessage('No workspace is open.');
				return;
			}

			const userQuestion = await vscode.window.showInputBox({
				placeHolder: 'What do you want to know about this project?',
				ignoreFocusOut: true,
			});

			if (!userQuestion || userQuestion.trim().length === 0) {
				return;
			}

			const aiDevConfig = await readAiDevConfig(workspaceRoot);
			if (!aiDevConfig.aiDevCorePathFromYaml) {
				await vscode.window.showErrorMessage('Missing aiDevCore.path in .ai-dev.yaml.');
				return;
			}
			const aiDevCorePathFromYaml = aiDevConfig.aiDevCorePathFromYaml;

			const modeResolution = getExecutionModeFromConfig(aiDevConfig);
			if ('errorMessage' in modeResolution) {
				await vscode.window.showErrorMessage(modeResolution.errorMessage);
				return;
			}

			if (modeResolution.mode === 'prompt-only') {
				try {
					const promptMarkdown = buildAnswerPromptMarkdown({
						workspaceRoot,
						aiDevCorePath: aiDevCorePathFromYaml,
						userQuestion: userQuestion.trim(),
					});
					await openMarkdownPromptAndCopy(promptMarkdown, 'Generated answer instructions and copied them to clipboard.');
				} catch (error) {
					if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
						await vscode.window.showErrorMessage('Missing .ai-dev.yaml in workspace root.');
						return;
					}

					const message = error instanceof Error ? error.message : String(error);
					await vscode.window.showErrorMessage(`Failed to generate answer instructions: ${message}`);
				}

				return;
			}

			try {
				const trimmedUserQuestion = userQuestion.trim();
				const aiDevCorePath = resolveAiDevCorePath(workspaceRoot, aiDevCorePathFromYaml);
				const workflowFilePath = path.join(aiDevCorePath, 'workflows/answer-docs/answer-from-ai-docs.md');
				const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');
				const docsDir = getConfiguredDocsDir(aiDevConfig);
				const docsDirAbsolutePath = path.resolve(workspaceRoot, docsDir);
				const rootSummaryPath = getRootSummaryFilePath(aiDevConfig);
				const rootSummaryAbsolutePath = path.resolve(workspaceRoot, rootSummaryPath);
				const [workflowFileContents, aiDevYamlContents, rootSummaryContents] = await Promise.all([
					fs.readFile(workflowFilePath, 'utf8'),
					fs.readFile(aiDevYamlPath, 'utf8'),
					readOptionalTextFile(rootSummaryAbsolutePath),
				]);
				const rootSummaryExists = rootSummaryContents !== undefined;
				const rootSummaryEmpty = rootSummaryExists && rootSummaryContents.trim().length === 0;
				const routedDocumentationContext = await collectRoutedDocumentationContextForAnswer({
					workspaceRoot,
					docsDirAbsolutePath,
					rootSummaryPath: rootSummaryAbsolutePath,
					userQuestion: trimmedUserQuestion,
				});

				const discoveredSummaryPaths = await discoverSummaryFilesRecursively(docsDirAbsolutePath);
				const discoveredSummaryCount = discoveredSummaryPaths.length;
				const routedSummaryAbsolutePaths = new Set(
					routedDocumentationContext.routedFiles
						.filter((file) => file.kind === 'summary')
						.map((file) => path.resolve(workspaceRoot, file.path))
				);
				const excludeFallbackAbsolutePaths = new Set<string>(routedSummaryAbsolutePaths);
				if (rootSummaryExists && !rootSummaryEmpty) {
					excludeFallbackAbsolutePaths.add(rootSummaryAbsolutePath);
				}

				let fallbackIncludedReason: string | undefined;
				if (!rootSummaryExists) {
					fallbackIncludedReason = 'Root summary file is missing.';
				} else if (rootSummaryEmpty) {
					fallbackIncludedReason = 'Root summary file is empty.';
				} else if (routedDocumentationContext.routedFiles.length === 0) {
					fallbackIncludedReason = 'Root summary routing returned no additional context for this question.';
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

				const directPromptMarkdown = buildAnswerFromAiDocsDirectPromptMarkdown({
					workspaceRoot,
					workflowFilePath,
					workflowFileContents,
					aiDevYamlPath,
					aiDevYamlContents,
					rootSummaryPath,
					rootSummaryExists,
					rootSummaryEmpty,
					rootSummaryContents,
					docsDir,
					discoveredSummaryCount,
					fallbackIncludedReason,
					routedDocumentationFiles: routedDocumentationContext.routedFiles,
					fallbackDiscoveredSummaries: fallbackDiscoveredSummaries.map((summary) => ({
						path: summary.relativePath,
						contents: truncateText(summary.contents, MAX_ROUTED_DOC_FILE_CHARS),
						score: summary.score,
					})),
					missingDocumentationPaths: routedDocumentationContext.missingPaths,
					userQuestion: trimmedUserQuestion,
				});

				const progressResult = await runDirectModelCallWithProgress({
					title: 'AI Dev: Answering from docs...',
					diagnosticTitle: 'AI Dev Answer From Docs Direct Diagnostic',
					buildPrompt: async () => directPromptMarkdown,
				});

				if (progressResult.status === 'cancelled') {
					await vscode.window.showInformationMessage('AI Dev answer from docs direct mode cancelled.');
					return;
				}

				if (progressResult.status === 'handled-error') {
					return;
				}

				if (progressResult.responseText.trim().length === 0) {
					await vscode.window.showWarningMessage('The selected language model returned an empty response. This can happen if Copilot/model services are still loading. Wait a moment and retry.');
				}

				await openDirectResultMarkdown({
					title: 'AI Dev Answer From Docs Direct Result',
					timestamp: progressResult.timestamp,
					modelDetails: progressResult.modelDetails,
					metadataLines: [
						'',
						`- User question: ${trimmedUserQuestion}`,
					],
					responseText: progressResult.responseText,
				});
				if (progressResult.responseText.length > 0) {
					await vscode.env.clipboard.writeText(progressResult.responseText);
				}
				await vscode.window.showInformationMessage(`AI Dev direct answer completed. Response length: ${progressResult.responseText.length}.`);
			} catch (error) {
				const { message, stack } = getErrorDetails(error);
				const timestamp = new Date().toISOString();
				const diagnosticMarkdown = [
					'# AI Dev Answer From Docs Direct Diagnostic',
					'',
					`- Timestamp: ${timestamp}`,
					`- Error message: ${message}`,
					'',
					'## Error stack',
					'',
					'```text',
					stack,
					'```',
				].join('\n');

				await openMarkdownDocument(diagnosticMarkdown);
				await vscode.window.showErrorMessage(`AI Dev answer from docs direct mode failed: ${message}`);
			}
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
		let initialSourceExcludeGlobs = [...FALLBACK_BATCH_EXCLUDE_GLOBS];
		try {
			const aiDevYamlContents = await fs.readFile(aiDevYamlPath, 'utf8');
			initialPromptOnly = getYamlNestedValue(aiDevYamlContents, 'aiProvider', 'mode')?.trim() === 'prompt-only';
			const configuredDocsDir = getYamlNestedValue(aiDevYamlContents, 'documentation', 'docsDir')?.trim();
			if (configuredDocsDir) {
				initialDocsDir = configuredDocsDir;
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
			initialSourceExcludeGlobs,
			onSave: async (settings: {
				promptOnly?: boolean;
				docsDir?: string;
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

	const setExecutionModeCommand = vscode.commands.registerCommand(
		SET_EXECUTION_MODE_COMMAND,
		openSettingsCommand
	);

	const copilotTestCommand = vscode.commands.registerCommand(
		COPILOT_TEST_COMMAND,
		async () => {
			try {
				const models = await vscode.lm.selectChatModels();
				if (models.length === 0) {
					const timestamp = new Date().toISOString();
					const diagnosticMarkdown = [
						'# AI Dev Copilot Batch Test Result',
						'',
						`- Timestamp: ${timestamp}`,
						`- Error: No models were returned by vscode.lm.selectChatModels().`,
					].join('\n');

					await vscode.window.showErrorMessage('No VS Code language model is available. Make sure a chat model is installed and enabled.');
					const doc = await vscode.workspace.openTextDocument({
						language: 'markdown',
						content: diagnosticMarkdown,
					});
					await vscode.window.showTextDocument(doc, { preview: false });
					await vscode.env.clipboard.writeText(diagnosticMarkdown);
					return;
				}

				const [model] = models;
				const expectedResponse1 = 'AI Dev Copilot batch test 1 received.';
				const expectedResponse2 = 'AI Dev Copilot batch test 2 received.';
				const requestPrompt1 = 'Respond with exactly this text and nothing else: AI Dev Copilot batch test 1 received.';
				const requestPrompt2 = 'Respond with exactly this text and nothing else: AI Dev Copilot batch test 2 received.';
				const timestamp = new Date().toISOString();
				const modelMetadata = model as unknown as {
					id?: unknown;
					name?: unknown;
					vendor?: unknown;
					family?: unknown;
				};
				const modelId = typeof modelMetadata.id === 'string' ? modelMetadata.id : 'n/a';
				const modelName = typeof modelMetadata.name === 'string' ? modelMetadata.name : 'n/a';
				const modelVendor = typeof modelMetadata.vendor === 'string' ? modelMetadata.vendor : 'n/a';
				const modelFamily = typeof modelMetadata.family === 'string' ? modelMetadata.family : 'n/a';

				const collectResponseText = async (prompt: string): Promise<string> => {
					const cancellation = new vscode.CancellationTokenSource();
					let response: vscode.LanguageModelChatResponse;
					try {
						response = await model.sendRequest(
							[
								vscode.LanguageModelChatMessage.User(prompt),
							],
							{},
							cancellation.token
						);
					} finally {
						cancellation.dispose();
					}

					let responseText = '';
					for await (const chunk of response.text) {
						responseText += chunk;
					}

					return responseText;
				};

				const responseText1 = await collectResponseText(requestPrompt1);
				const responseText2 = await collectResponseText(requestPrompt2);
				const responseLength1 = responseText1.length;
				const responseLength2 = responseText2.length;
				const exactMatch1 = responseText1 === expectedResponse1;
				const exactMatch2 = responseText2 === expectedResponse2;
				const overallPass = exactMatch1 && exactMatch2;
				const diagnosticMarkdown = [
					'# AI Dev Copilot Batch Test Result',
					'',
					`- Timestamp: ${timestamp}`,
					'- Selected model info:',
					`  - id: ${modelId}`,
					`  - name: ${modelName}`,
					`  - vendor: ${modelVendor}`,
					`  - family: ${modelFamily}`,
					`- Request 1 expected text: ${expectedResponse1}`,
					`- Request 1 response length: ${responseLength1}`,
					`- Request 1 exact-match: ${exactMatch1}`,
					'',
					'## Request 1 raw response',
					'',
					'```text',
					responseText1,
					'```',
					'',
					`- Request 2 expected text: ${expectedResponse2}`,
					`- Request 2 response length: ${responseLength2}`,
					`- Request 2 exact-match: ${exactMatch2}`,
					'',
					'## Request 2 raw response',
					'',
					'```text',
					responseText2,
					'```',
					'',
					`- Overall pass: ${overallPass}`,
				].join('\n');

				const doc = await vscode.workspace.openTextDocument({
					language: 'markdown',
					content: diagnosticMarkdown,
				});
				await vscode.window.showTextDocument(doc, { preview: false });
				await vscode.env.clipboard.writeText(diagnosticMarkdown);
				await vscode.window.showInformationMessage(`Copilot batch test completed. Overall pass: ${overallPass}.`);
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				const stack = error instanceof Error && typeof error.stack === 'string' ? error.stack : 'n/a';
				const timestamp = new Date().toISOString();
				const diagnosticMarkdown = [
					'# AI Dev Copilot Batch Test Result',
					'',
					`- Timestamp: ${timestamp}`,
					`- Error message: ${message}`,
					'',
					'## Error stack',
					'',
					'```text',
					stack,
					'```',
				].join('\n');
				const doc = await vscode.workspace.openTextDocument({
					language: 'markdown',
					content: diagnosticMarkdown,
				});
				await vscode.window.showTextDocument(doc, { preview: false });
				await vscode.env.clipboard.writeText(diagnosticMarkdown);
				await vscode.window.showErrorMessage(`Copilot test failed: ${message}`);
			}
		}
	);

	context.subscriptions.push(
		selectWorkflowCommand,
		generateUnitDocCommand,
		generateUnitDocsBatchExperimentalCommand,
		generateArchitectureSummaryCommand,
		updateFolderSummaryCommand,
		reviewDocumentationCommand,
		reviewFileDocumentationCommand,
		answerFromAiDocsCommand,
		settingsCommand,
		setExecutionModeCommand,
		copilotTestCommand
	);
}

export function deactivate() {}
