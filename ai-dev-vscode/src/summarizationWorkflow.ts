import * as fs from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {
	getAiDevYamlPromptSection,
	getArchitectureSummaryPath,
	getExecutionModeFromConfig,
	readAiDevConfig,
	resolveAiDevCorePath,
	type AiDevConfig,
} from './config';
import {
	readOptionalTextFile,
	truncateText,
} from './fileUtilities';
import {
	readDependencyMap,
} from './dependencyMap';
import {
	hydrateSummarizationDependencyContext,
} from './summarizationDependencyContext';
import {
	refreshJenkinsDependencyMapForSummarization,
} from './dependencyMapWorkflow';
import {
	MAX_DIRECT_FILE_CHARS,
} from './modelContextLimits';
import {
	matchesAnyGlob,
} from './pathMatching';
import {
	appendDependencyContextToDirectPromptMarkdown,
	buildGenerateArchitectureSummaryDirectPromptMarkdown,
	buildGroupedGenerateUnitDocDirectPromptMarkdown,
} from './promptBuilder';
import {
	discoverBatchUnitDocCandidates,
	getConfiguredDocsDir,
	isPathInsideDirectory,
	normalizeBatchSourceGlob,
} from './sourceDiscovery';
import {
	injectSummarizationInstructions,
	readSummarizationConfig,
	resolveSummarizationInstructions,
} from './summarizationConfig';
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
	getExpectedDirectorySummaryPath,
	getOpenWorkspaceRoot,
	getSelectedSourcePath,
	normalizePathForMarkdown,
} from './workspace';
let allowDocsDirWritesForSession = false;

function formatRelativePath(workspaceRoot: string, absolutePath: string): string {
	return normalizePathForMarkdown(path.relative(workspaceRoot, absolutePath));
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

export async function previewSummarizationTarget(
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

export async function executeSummarizationTarget(
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
	dependencyMapRefreshed: boolean;
	dependencyMapRefreshWarnings: string[];
	dependencyFilesIncluded: number;
	dependencyFiles: Array<{
		primarySourcePath: string;
		path: string;
		relationshipKind: string;
		resolution: 'exact' | 'inferred';
		evidence: string[];
	}>;
	dependencyWarnings: string[];
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

	const selectedSourcePaths = [
		...new Set(
			generateActions
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
	].sort((left, right) =>
		left.localeCompare(right)
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

	const allConfiguredSourcePathsByDoc = new Map<
		string,
		string[]
	>();

	const allConfiguredCandidates =
		await discoverBatchUnitDocCandidates(
			workspaceRoot,
			aiDevConfig
		);

	for (const sourceAbsolutePath of allConfiguredCandidates) {
		const sourcePath = normalizePathForMarkdown(
			path.relative(workspaceRoot, sourceAbsolutePath)
		);
		const docPath = getExpectedDirectorySummaryPath({
			workspaceRoot,
			sourceFilePath: sourceAbsolutePath,
			docsDir: getConfiguredDocsDir(aiDevConfig),
		});
		const existing =
			allConfiguredSourcePathsByDoc.get(docPath);

		if (existing) {
			existing.push(sourcePath);
		} else {
			allConfiguredSourcePathsByDoc.set(
				docPath,
				[sourcePath]
			);
		}
	}

	for (const sourcePaths of allConfiguredSourcePathsByDoc.values()) {
		sourcePaths.sort((left, right) =>
			left.localeCompare(right)
		);
	}

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
		dependencyMapRefreshed: false,
		dependencyMapRefreshWarnings: [] as string[],
		dependencyFilesIncluded: 0,
		dependencyFiles: [] as Array<{
			primarySourcePath: string;
			path: string;
			relationshipKind: string;
			resolution: 'exact' | 'inferred';
			evidence: string[];
		}>,
		dependencyWarnings: [] as string[],
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

	const dependencyMapPreflight =
		await refreshJenkinsDependencyMapForSummarization(
			workspaceRoot,
			selectedSourcePaths
		);

	result.dependencyMapRefreshed =
		dependencyMapPreflight.refreshed;
	result.dependencyMapRefreshWarnings.push(
		...dependencyMapPreflight.warnings
	);

	const dependencyMap = await readDependencyMap(
		workspaceRoot,
		aiDevConfig
	);

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

			const resolvedBySource = sourcePaths.map(
				(sourcePath) =>
					resolveSummarizationInstructions(
						summarizationConfig,
						sourcePath
					)
			);

			const dependencyContext =
				await hydrateSummarizationDependencyContext({
					workspaceRoot,
					dependencyMap,
					sourcePaths,
					resolvedBySource,
				});

			result.dependencyFilesIncluded +=
				dependencyContext.files.length;
			result.dependencyFiles.push(
				...dependencyContext.files.map(
					(file) => ({
						primarySourcePath:
							file.primarySourcePath,
						path: file.path,
						relationshipKind:
							file.relationshipKind,
						resolution: file.resolution,
						evidence: [...file.evidence],
					})
				)
			);
			result.dependencyWarnings.push(
				...dependencyContext.warnings
			);

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
					dependencyContextFiles:
						dependencyContext.files,
					sourceSetIsAuthoritative: (() => {
						const allSourcePaths =
							allConfiguredSourcePathsByDoc.get(
								docPath
							) ?? [];

						return allSourcePaths.length === sourcePaths.length
							&& allSourcePaths.every(
								(sourcePath, index) =>
									sourcePath === sourcePaths[index]
							);
					})(),
				});

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

export async function prepareSingleFileSummary(
	target: string
): Promise<{
	prompt: string;
	sourcePath: string;
	outputPath: string;
	warnings: string[];
	dependencyMapRefreshed: boolean;
	dependencyFiles: Array<{
		primarySourcePath: string;
		path: string;
		relationshipKind: string;
		resolution: 'exact' | 'inferred';
		evidence: string[];
	}>;
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

	const dependencyMapPreflight =
		await refreshJenkinsDependencyMapForSummarization(
			workspaceRoot,
			[bundle.selectedSourcePath]
		);

	const dependencyMap = await readDependencyMap(
		workspaceRoot,
		aiDevConfig
	);

	const dependencyContext =
		await hydrateSummarizationDependencyContext({
			workspaceRoot,
			dependencyMap,
			sourcePaths: [bundle.selectedSourcePath],
			resolvedBySource: [resolvedInstructions],
		});

	const enrichedPrompt =
		appendDependencyContextToDirectPromptMarkdown(
			bundle.directPromptMarkdown,
			dependencyContext.files
		);

	return {
		prompt: injectSummarizationInstructions(
			enrichedPrompt,
			resolvedInstructions
		),
		sourcePath: bundle.selectedSourcePath,
		outputPath: bundle.expectedSummaryPath,
		warnings: [
			...dependencyMapPreflight.warnings,
			...dependencyContext.warnings,
		],
		dependencyMapRefreshed:
			dependencyMapPreflight.refreshed,
		dependencyFiles:
			dependencyContext.files.map(
				(file) => ({
					primarySourcePath:
						file.primarySourcePath,
					path: file.path,
					relationshipKind:
						file.relationshipKind,
					resolution: file.resolution,
					evidence: [...file.evidence],
				})
			),
	};
}

export async function completeSingleFileSummary(
	preparation: {
		prompt: string;
		sourcePath: string;
		outputPath: string;
		warnings: string[];
		dependencyMapRefreshed: boolean;
		dependencyFiles: Array<{
			primarySourcePath: string;
			path: string;
			relationshipKind: string;
			resolution: 'exact' | 'inferred';
			evidence: string[];
		}>;
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
