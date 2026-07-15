import * as fs from 'node:fs/promises';
import type { Dirent } from 'node:fs';
import * as path from 'node:path';
import {
	getAiDevYamlPromptSection,
	getLegacyRootSummaryFilePath,
	getRootSummaryFilePath,
	readAiDevConfig,
	resolveAiDevCorePath,
} from './config';
import {
	readOptionalTextFile,
	truncateText,
} from './fileUtilities';
import {
	buildAnswerFromAiDocsDirectPromptMarkdown,
} from './promptBuilder';
import {
	getConfiguredDocsDir,
	isPathInsideDirectory,
} from './sourceDiscovery';
import {
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
} from './workspace';
const MAX_ROUTED_DOC_INDEX_FILES = 12;

const MAX_ROUTED_DOC_FILES = 24;

const MAX_ROUTED_DOC_FILE_CHARS = 6000;

const MAX_ROUTED_DOC_TOTAL_CHARS = 90000;

const MAX_FALLBACK_DISCOVERED_SUMMARIES = 6;

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

export async function buildSummaryAnswerRoute(
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
