import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import type { AssistantReviewRequest } from './assistantTerminal';
import {
	ARCHITECTURE_SUMMARY_FILE_NAME,
	getAiDevYamlPromptSection,
	getExecutionModeFromConfig,
	getRootSummaryFilePath,
	readAiDevConfig,
	resolveAiDevCorePath,
	type AiDevConfig,
} from './config';
import {
	fileExists,
	readOptionalTextFile,
	truncateText,
} from './fileUtilities';
import {
	existingChangedFilesWithContent,
	getGitChangedFiles,
	getGitDiffForFiles,
	getGitRenameRecords,
	type GitFileDiff,
	type GitRenameRecord,
} from './git';
import {
	MAX_DIRECT_CHANGED_FILE_CONTENTS,
	MAX_DIRECT_DIFF_CHARS,
	MAX_DIRECT_DIFF_SAMPLE_FILES,
	MAX_DIRECT_EXPECTED_DOC_CONTEXT_FILES,
	MAX_DIRECT_FILE_CHARS,
	MAX_DIRECT_SCOPED_INDEX_CONTEXT_FILES,
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
} from './sourceDiscovery';
import {
	getExpectedDirectorySummaryPath,
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
} from './workspace';
function normalizePathForReview(filePath: string): string {
	return normalizePathForMarkdown(filePath)
		.replace(/^\.\//, '');
}

function isTestFilePath(filePath: string): boolean {
	const normalized =
		normalizePathForReview(filePath).toLowerCase();

	return (
		/(^|\/)(test|tests|spec|specs)\//.test(normalized)
		|| /\.(test|spec)\.[^/]+$/.test(normalized)
		|| /(^|\/)__tests__\//.test(normalized)
	);
}

export function matchesReviewTarget(
	filePath: string,
	target?: string
): boolean {
	if (!target?.trim()) {
		return true;
	}

	const normalizedPath =
		normalizePathForReview(filePath);
	const normalizedTarget =
		normalizePathForReview(target.trim())
			.replace(/\/+$/, '');

	if (/[*?\[\]{}]/.test(normalizedTarget)) {
		return globToRegExp(normalizedTarget).test(
			normalizedPath
		);
	}

	return (
		normalizedPath === normalizedTarget
		|| normalizedPath.startsWith(
			`${normalizedTarget}/`
		)
	);
}

export interface ReviewFileSelection {
	implementationPaths: string[];
	testPaths: string[];
	selectedPaths: string[];
}

export function selectReviewFiles(params: {
	candidateFilePaths: string[];
	mode: 'code' | 'tests';
	docsDir: string;
	target?: string;
	artifactExcludeGlobs?: string[];
}): ReviewFileSelection {
	const normalizedDocsDir =
		normalizePathForReview(params.docsDir)
			.replace(/\/+$/, '');

	const artifactExcludeGlobs =
		params.artifactExcludeGlobs
		?? NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS;

	const reviewablePaths = [
		...new Set(
			params.candidateFilePaths
				.map(normalizePathForReview)
				.filter(
					(filePath) =>
						filePath !== normalizedDocsDir
						&& !filePath.startsWith(
							`${normalizedDocsDir}/`
						)
						&& !matchesAnyGlob(
							filePath,
							artifactExcludeGlobs
						)
						&& matchesReviewTarget(
							filePath,
							params.target
						)
				)
		),
	];

	const implementationPaths =
		reviewablePaths.filter(
			(filePath) => !isTestFilePath(filePath)
		);

	const testPaths =
		reviewablePaths.filter(isTestFilePath);

	return {
		implementationPaths,
		testPaths,
		selectedPaths:
			params.mode === 'code'
				? implementationPaths
				: reviewablePaths,
	};
}

export function selectChangedReviewFiles(params: {
	changedFilePaths: string[];
	mode: 'code' | 'tests';
	docsDir: string;
	target?: string;
	artifactExcludeGlobs?: string[];
}): ReviewFileSelection {
	return selectReviewFiles({
		candidateFilePaths: params.changedFilePaths,
		mode: params.mode,
		docsDir: params.docsDir,
		target: params.target,
		artifactExcludeGlobs:
			params.artifactExcludeGlobs,
	});
}
function formatRelativePath(
	workspaceRoot: string,
	absolutePath: string
): string {
	return normalizePathForMarkdown(
		path.relative(workspaceRoot, absolutePath)
	);
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

export async function previewProjectReview(
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

export async function prepareProjectReview(
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
