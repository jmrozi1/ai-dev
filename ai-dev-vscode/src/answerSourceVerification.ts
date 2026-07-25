import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	type DependencyMap,
	findOutgoingDependencyEdges,
} from './dependencyMap';
import {
	allocateSourceEvidenceChunks,
	buildSourceEvidenceCandidatesForFile,
	classifySourceEvidenceAuthority,
	tokenizeSourceEvidenceQuestionTerms,
	tokenizeSourceEvidenceTextTerms,
	type SourceEvidenceAuthority,
	type SourceEvidenceMethod,
	type SourceEvidenceRole,
	type SourceEvidenceTarget,
} from './answerSourceEvidence';
import {
	isPathInsideDirectory,
} from './sourceDiscovery';
import {
	normalizePathForMarkdown,
} from './workspace';

const MAX_DISCOVERED_PRIMARY_SOURCE_TARGETS = 128;
const MAX_DISCOVERED_DEPENDENCY_SOURCE_TARGETS = 256;
const MAX_DISCOVERY_READ_BYTES = 2_000_000;
const MAX_VERIFIED_FILE_CHARS = 12000;
const MAX_VERIFIED_TOTAL_CHARS = 60000;
const MAX_VERIFIED_PER_FILE_CHARS = 20000;
const MAX_TOP_RANKED_TARGETS = 10;
const MAX_COUNTED_OCCURRENCES_PER_TERM = 4;

export interface AnswerSummaryEvidence {
	path: string;
	contents: string;
}

export interface VerifiedAnswerSourceChunk {
	path: string;
	role: SourceEvidenceRole;
	authority: SourceEvidenceAuthority;
	primarySourcePath?: string;
	relationshipKind?: string;
	resolution?: 'exact' | 'inferred';
	evidence?: string[];
	symbol?: string;
	startLine: number;
	endLine: number;
	extractionMethod: SourceEvidenceMethod;
	includedChars: number;
	clippedByBudget: boolean;
	contents: string;
}

export type VerifiedAnswerSourceWarningLevel =
	| 'info'
	| 'failure';

export type VerifiedAnswerSourceWarningCode =
	| 'source_clipped'
	| 'dependency_clipped'
	| 'source_missing'
	| 'source_unreadable'
	| 'dependency_unresolved'
	| 'dependency_unavailable'
	| 'dependency_unreadable'
	| 'symbol_unresolved';

export type VerifiedAnswerSourceFailureWarningCode =
	| 'source_missing'
	| 'source_unreadable'
	| 'dependency_unresolved'
	| 'dependency_unavailable'
	| 'dependency_unreadable';

export interface VerifiedAnswerSourceWarning {
	level: VerifiedAnswerSourceWarningLevel;
	code: VerifiedAnswerSourceWarningCode;
	message: string;
	path?: string;
	primarySourcePath?: string;
}

export interface VerifiedAnswerSourceFailureWarning
	extends VerifiedAnswerSourceWarning {
	code: VerifiedAnswerSourceFailureWarningCode;
}

export interface VerifiedAnswerSourceContext {
	files: VerifiedAnswerSourceChunk[];
	warnings: string[];
	warningDetails: VerifiedAnswerSourceWarning[];
	sourceTargetsDiscovered: number;
	sourceFilesRead: number;
	sourceFilesConsidered: number;
	sourceChunkCandidates: number;
	sourceChunksIncluded: number;
	verifiedSourceCharactersIncluded: number;
	sourceChunksClippedByBudget: number;
	wholeFileFallbackChunks: number;
	verifiedSourceChunkCount: number;
	verifiedSourceUniqueFileCount: number;
	sourceSymbolsRequested: number;
	sourceSymbolsResolved: number;
	sourceDeclarationChunksGenerated: number;
	sourceDeclarationChunksIncluded: number;
	sourceUnresolvedSymbols: number;
	topRankedTargets: Array<{
		path: string;
		targetScore: number;
		authority: SourceEvidenceAuthority;
		symbol?: string;
		method?: SourceEvidenceMethod;
		included: boolean;
	}>;
}

interface ExtractedVerificationCandidate {
	path: string;
	markedAsSourcePath: boolean;
	symbol?: string;
	symbolAssociation?: 'explicit' | 'inferred';
	encounterOrder: number;
	referenceCount: number;
	pathTermMatches: number;
	contextTermMatches: number;
}

const VERIFIED_SOURCE_FILE_EXTENSIONS = [
	'.ts',
	'.tsx',
	'.js',
	'.jsx',
	'.json',
	'.md',
	'.yaml',
	'.yml',
	'.xml',
	'.sh',
	'.bash',
	'.groovy',
	'.java',
	'.py',
	'.go',
	'.rs',
	'.cs',
	'.cpp',
	'.c',
	'.h',
	'.hpp',
	'.html',
	'.css',
	'.scss',
];

const VERIFIED_SOURCE_FAILURE_CODES = new Set<VerifiedAnswerSourceFailureWarningCode>([
	'source_missing',
	'source_unreadable',
	'dependency_unresolved',
	'dependency_unavailable',
	'dependency_unreadable',
]);

export function isVerifiedAnswerSourceFailureCode(
	code: VerifiedAnswerSourceWarningCode
): code is VerifiedAnswerSourceFailureWarningCode {
	return VERIFIED_SOURCE_FAILURE_CODES.has(
		code as VerifiedAnswerSourceFailureWarningCode
	);
}

export function collectVerifiedAnswerSourceFailures(
	warnings: VerifiedAnswerSourceWarning[]
): VerifiedAnswerSourceFailureWarning[] {
	return warnings.filter(
		(warning): warning is VerifiedAnswerSourceFailureWarning =>
			isVerifiedAnswerSourceFailureCode(
				warning.code
			)
	);
}

function normalizeVerificationCandidatePath(
	candidate: string
): string {
	return normalizePathForMarkdown(candidate)
		.replace(/^\.\/+/, '')
		.replace(/^\/+/, '');
}

function countQuestionTermMatches(
	text: string,
	questionTerms: string[]
): number {
	if (!text || questionTerms.length === 0) {
		return 0;
	}

	const lowerText = text.toLowerCase();
	let count = 0;

	for (const term of questionTerms) {
		if (lowerText.includes(term)) {
			count += 1;
		}
	}

	return count;
}

function countTokenOverlap(
	text: string,
	questionTerms: string[]
): number {
	if (!text || questionTerms.length === 0) {
		return 0;
	}

	const textTerms = new Set(
		tokenizeSourceEvidenceTextTerms(text)
	);
	let overlapCount = 0;

	for (const term of questionTerms) {
		if (textTerms.has(term)) {
			overlapCount += 1;
		}
	}

	return overlapCount;
}

function countBoundedTermOccurrences(
	text: string,
	questionTerms: string[]
): number {
	if (!text || questionTerms.length === 0) {
		return 0;
	}

	const lowerText = text.toLowerCase();
	let total = 0;

	for (const term of questionTerms) {
		let termCount = 0;
		let startIndex = 0;

		while (termCount < MAX_COUNTED_OCCURRENCES_PER_TERM) {
			const index = lowerText.indexOf(term, startIndex);
			if (index === -1) {
				break;
			}

			termCount += 1;
			startIndex = index + term.length;
		}

		total += termCount;
	}

	return total;
}

function getLineContextAtIndex(
	contents: string,
	matchIndex: number
): string {
	const lineStart =
		contents.lastIndexOf('\n', matchIndex) + 1;
	const lineEndCandidate = contents.indexOf(
		'\n',
		matchIndex
	);
	const lineEnd =
		lineEndCandidate === -1
			? contents.length
			: lineEndCandidate;

	return contents.slice(lineStart, lineEnd);
}

function scorePrimaryDiscoveryTarget(
	candidate: ExtractedVerificationCandidate,
	questionTerms: string[]
): number {
	let score = 0;
	const symbolTermMatches = countTokenOverlap(
		candidate.symbol ?? '',
		questionTerms
	);

	score += candidate.markedAsSourcePath ? 120 : 40;
	score += candidate.pathTermMatches * 80;
	score += candidate.contextTermMatches * 30;
	score += candidate.symbol ? 120 : 0;
	score += candidate.symbolAssociation === 'explicit' ? 140 : 0;
	score += candidate.symbolAssociation === 'inferred' ? 70 : 0;
	score += symbolTermMatches * 70;
	score += candidate.referenceCount * 5;

	return score;
}

function scoreDependencyDiscoveryTarget(params: {
	role: SourceEvidenceRole;
	authority: SourceEvidenceAuthority;
	resolution?: 'exact' | 'inferred';
	primaryTargetScore: number;
	evidenceTermsScore: number;
	pathTermMatches: number;
}): number {
	let score = 0;

	score += Math.floor(params.primaryTargetScore * 0.5);
	score += params.pathTermMatches * 70;
	score += params.evidenceTermsScore * 20;
	score += params.role === 'primary-source' ? 10 : 0;
	score += params.authority === 'implementation' ? 80 : 0;
	score += params.authority === 'test' ? -80 : 0;
	score += params.authority === 'generated-vendor' ? -120 : 0;
	score += params.resolution === 'exact' ? 6 : 0;
	score += params.resolution === 'inferred' ? 3 : 0;

	return score;
}

export function isFileVerificationCandidate(params: {
	candidate: string;
	markedAsSourcePath?: boolean;
}): boolean {
	const candidate = params.candidate.trim();

	if (!candidate) {
		return false;
	}

	if (params.markedAsSourcePath) {
		return true;
	}

	if (
		candidate.includes('/')
		|| candidate.includes('\\')
	) {
		return true;
	}

	const lowerCandidate = candidate.toLowerCase();

	return VERIFIED_SOURCE_FILE_EXTENSIONS.some(
		(extension) => lowerCandidate.endsWith(extension)
	);
}

function extractVerificationCandidates(params: {
	contents: string;
	questionTerms: string[];
}): ExtractedVerificationCandidate[] {
	const candidates = new Map<string, ExtractedVerificationCandidate>();
	const regex = /`([^`\r\n]+)`/g;
	let match: RegExpExecArray | null;
	let encounterOrder = candidates.size;
	const pendingStructuredPathByLine = new Map<number, string>();

	const isLikelySymbolHint = (value: string): boolean => {
		if (!value) {
			return false;
		}

		const trimmed = value.trim();
		if (!trimmed) {
			return false;
		}

		if (
			trimmed.includes('/')
			|| trimmed.includes('\\')
			|| trimmed.includes(' ')
			|| trimmed.includes(':')
		) {
			return false;
		}

		if (/^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?$/.test(trimmed)) {
			return true;
		}

		return false;
	};

	const lineOfIndex = (index: number): number =>
		params.contents.slice(0, index).split('\n').length - 1;

	const upsertCandidate = (
		rawCandidate: string,
		markedAsSourcePath: boolean,
		pathTermMatches: number,
		contextTermMatches: number,
		symbol?: string,
		symbolAssociation?: 'explicit' | 'inferred'
	): void => {
		const normalized = normalizeVerificationCandidatePath(rawCandidate.trim());

		if (!normalized) {
			return;
		}

		const existing = candidates.get(normalized);

		if (existing) {
			existing.markedAsSourcePath =
				existing.markedAsSourcePath || markedAsSourcePath;
			if (
				!existing.symbol
				|| (existing.symbolAssociation === 'inferred'
					&& symbolAssociation === 'explicit')
			) {
				existing.symbol = symbol ?? existing.symbol;
				existing.symbolAssociation = symbolAssociation
					?? existing.symbolAssociation;
			}
			existing.referenceCount += 1;
			existing.pathTermMatches = Math.max(
				existing.pathTermMatches,
				pathTermMatches
			);
			existing.contextTermMatches = Math.max(
				existing.contextTermMatches,
				contextTermMatches
			);
			return;
		}

		candidates.set(normalized, {
			path: normalized,
			markedAsSourcePath,
			symbol,
			symbolAssociation,
			encounterOrder,
			referenceCount: 1,
			pathTermMatches,
			contextTermMatches,
		});
		encounterOrder += 1;
	};

	while ((match = regex.exec(params.contents)) !== null) {
		const candidate = match[1]?.trim();

		if (
			!candidate
			|| candidate.includes('://')
			|| candidate.startsWith('-')
			|| candidate.includes('(')
			|| candidate.includes(')')
			|| candidate.includes(' ')
		) {
			continue;
		}

		const lineContext = getLineContextAtIndex(
			params.contents,
			match.index
		);
		const pathTermMatches = countTokenOverlap(
			candidate,
			params.questionTerms
		);
		const contextTermMatches = countTokenOverlap(
			lineContext,
			params.questionTerms
		) + countBoundedTermOccurrences(
			lineContext,
			params.questionTerms
		);

		upsertCandidate(
			candidate,
			false,
			pathTermMatches,
			contextTermMatches
		);
	}

	const structuredPathRegex =
		/(?:"(?:sourcePath|source_path|sourceFile|source_file)"\s*:\s*"([^"\r\n]+)")|(?:\b(?:sourcePath|source_path|sourceFile|source_file)\s*:\s*([^\s#`][^\r\n`]*))/g;
	let structuredMatch: RegExpExecArray | null;

	while ((structuredMatch = structuredPathRegex.exec(params.contents)) !== null) {
		const metadataPath =
			structuredMatch[1]
			?? structuredMatch[2]
			?? '';

		if (!metadataPath) {
			continue;
		}

		const matchText = structuredMatch[0] ?? metadataPath;
		const lineNumber = lineOfIndex(structuredMatch.index);
		const lineContext = getLineContextAtIndex(
			params.contents,
			structuredMatch.index
		);
		const pathTermMatches = countTokenOverlap(
			metadataPath,
			params.questionTerms
		);
		const contextTermMatches = countTokenOverlap(
			lineContext,
			params.questionTerms
		) + countBoundedTermOccurrences(matchText, params.questionTerms);

		upsertCandidate(
			metadataPath,
			true,
			pathTermMatches,
			contextTermMatches
		);

		pendingStructuredPathByLine.set(lineNumber, metadataPath);
	}

	const lineArray = params.contents.split('\n');
	for (let index = 0; index < lineArray.length; index += 1) {
		const line = lineArray[index];
		const backtickedTokens = [...line.matchAll(/`([^`\r\n]+)`/g)]
			.map((item) => item[1].trim())
			.filter(Boolean);

		if (backtickedTokens.length >= 2) {
			const pathToken = backtickedTokens.find((token) =>
				isFileVerificationCandidate({ candidate: token })
			);
			const symbolToken = backtickedTokens.find(
				(token) => token !== pathToken && isLikelySymbolHint(token)
			);

			if (pathToken && symbolToken) {
				const pathTermMatches = countTokenOverlap(
					pathToken,
					params.questionTerms
				);
				const contextTermMatches = countTokenOverlap(
					line,
					params.questionTerms
				) + countBoundedTermOccurrences(
					line,
					params.questionTerms
				);

				upsertCandidate(
					pathToken,
					false,
					pathTermMatches,
					contextTermMatches,
					symbolToken,
					'inferred'
				);
			}
		}

		const structuredSymbolMatch = line.match(
			/(?:"symbol"\s*:\s*"([^"\r\n]+)")|(?:\bsymbol\s*:\s*`?([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?)`?)/
		);
		const structuredSymbol =
			structuredSymbolMatch?.[1]
			?? structuredSymbolMatch?.[2];

		if (structuredSymbol && isLikelySymbolHint(structuredSymbol)) {
			const sameLinePath = pendingStructuredPathByLine.get(index);
			const adjacentPath = pendingStructuredPathByLine.get(index - 1);
			const targetPath = sameLinePath ?? adjacentPath;

			if (targetPath) {
				const pathTermMatches = countTokenOverlap(
					targetPath,
					params.questionTerms
				);
				const contextTermMatches = countTokenOverlap(
					line,
					params.questionTerms
				) + countBoundedTermOccurrences(
					line,
					params.questionTerms
				);

				upsertCandidate(
					targetPath,
					true,
					pathTermMatches,
					contextTermMatches,
					structuredSymbol,
					'explicit'
				);
			}
		}
	}

	return [...candidates.values()];
}

async function readVerifiedFile(params: {
	workspaceRoot: string;
	docsDirAbsolutePath: string;
	relativePath: string;
}): Promise<
	| {
		status: 'ok';
		contents: string;
	}
	| {
		status: 'missing';
	}
	| {
		status: 'unreadable';
		reason: string;
	}
	| {
		status: 'excluded';
	}
> {
	const absolutePath = path.resolve(
		params.workspaceRoot,
		params.relativePath
	);

	if (
		!isPathInsideDirectory(
			absolutePath,
			params.workspaceRoot
		)
		|| isPathInsideDirectory(
			absolutePath,
			params.docsDirAbsolutePath
		)
	) {
		return {
			status: 'excluded',
		};
	}

	let stat;

	try {
		stat = await fs.stat(absolutePath);
	} catch (error) {
		const nodeError = error as NodeJS.ErrnoException;

		if (nodeError?.code === 'ENOENT') {
			return {
				status: 'missing',
			};
		}

		return {
			status: 'unreadable',
			reason:
				error instanceof Error
					? error.message
					: String(error),
		};
	}

	if (!stat.isFile()) {
		return {
			status: 'missing',
		};
	}

	let contents: string;

	try {
		contents = await fs.readFile(
			absolutePath,
			'utf8'
		);
	} catch (error) {
		return {
			status: 'unreadable',
			reason:
				error instanceof Error
					? error.message
					: String(error),
		};
	}

	return {
		status: 'ok',
		contents,
	};
}

export async function collectVerifiedAnswerSourceContext(
	params: {
		workspaceRoot: string;
		docsDir: string;
		summaryEvidence: AnswerSummaryEvidence[];
		userQuestion?: string;
		dependencyMap: DependencyMap;
	}
): Promise<VerifiedAnswerSourceContext> {
	const docsDirAbsolutePath = path.resolve(
		params.workspaceRoot,
		params.docsDir
	);
	const files: VerifiedAnswerSourceChunk[] = [];
	const warningDetails: VerifiedAnswerSourceWarning[] = [];
	const warningDetailKeys = new Set<string>();
	const sourceTargetsByPath = new Map<string, SourceEvidenceTarget>();
	const readableSourceTargets: SourceEvidenceTarget[] = [];
	const readableSourceContents = new Map<string, string>();
	const dependencyEvidenceByPath = new Map<string, string[]>();
	const primaryTargetScores = new Map<string, number>();
	const questionTerms = tokenizeSourceEvidenceQuestionTerms(
		params.userQuestion ?? ''
	);
	let totalDiscoveryReadBytes = 0;
	let sourceSymbolsRequested = 0;
	let sourceSymbolsResolved = 0;
	let sourceDeclarationChunksGenerated = 0;
	let sourceDeclarationChunksIncluded = 0;
	let sourceUnresolvedSymbols = 0;

	const addWarning = (
		warning: VerifiedAnswerSourceWarning
	): void => {
		const key = `${warning.level}|${warning.code}|${warning.message}`;

		if (warningDetailKeys.has(key)) {
			return;
		}

		warningDetailKeys.add(key);
		warningDetails.push(warning);
	};

	const candidatePathMap = new Map<string, ExtractedVerificationCandidate>();

	for (const evidence of params.summaryEvidence) {
		for (const candidate of extractVerificationCandidates({
			contents: evidence.contents,
			questionTerms,
		})) {
			const existingCandidate = candidatePathMap.get(candidate.path);

			if (existingCandidate) {
				existingCandidate.markedAsSourcePath =
					existingCandidate.markedAsSourcePath
					|| candidate.markedAsSourcePath;
				existingCandidate.referenceCount +=
					candidate.referenceCount;
				existingCandidate.pathTermMatches = Math.max(
					existingCandidate.pathTermMatches,
					candidate.pathTermMatches
				);
				existingCandidate.contextTermMatches = Math.max(
					existingCandidate.contextTermMatches,
					candidate.contextTermMatches
				);
				continue;
			}

			candidatePathMap.set(candidate.path, candidate);
		}
	}

	const candidatePaths = [...candidatePathMap.values()]
		.filter((candidatePath) =>
			isFileVerificationCandidate({
				candidate: candidatePath.path,
				markedAsSourcePath: candidatePath.markedAsSourcePath,
			})
		)
		.sort((left, right) => {
			const scoreDifference =
				scorePrimaryDiscoveryTarget(right, questionTerms)
				- scorePrimaryDiscoveryTarget(left, questionTerms);
			if (scoreDifference !== 0) {
				return scoreDifference;
			}

			const pathDifference = left.path.localeCompare(right.path);
			if (pathDifference !== 0) {
				return pathDifference;
			}

			return left.encounterOrder - right.encounterOrder;
		})
		.slice(0, MAX_DISCOVERED_PRIMARY_SOURCE_TARGETS);

	for (const candidatePath of candidatePaths) {
		if (
			totalDiscoveryReadBytes >= MAX_DISCOVERY_READ_BYTES
		) {
			break;
		}

		const targetScore = scorePrimaryDiscoveryTarget(
			candidatePath,
			questionTerms
		);

		const verified = await readVerifiedFile({
			workspaceRoot: params.workspaceRoot,
			docsDirAbsolutePath,
			relativePath: candidatePath.path,
		});

		if (verified.status === 'excluded') {
			continue;
		}

		if (verified.status === 'missing') {
			addWarning({
				level: 'failure',
				code: 'source_missing',
				message:
					`${candidatePath.path}: referenced source path was not found for answer verification.`,
				path: candidatePath.path,
			});
			continue;
		}

		if (verified.status === 'unreadable') {
			addWarning({
				level: 'failure',
				code: 'source_unreadable',
				message:
					`${candidatePath.path}: unable to read source for answer verification: ${verified.reason}`,
				path: candidatePath.path,
			});
			continue;
		}

		totalDiscoveryReadBytes += Buffer.byteLength(
			verified.contents,
			'utf8'
		);

		if (sourceTargetsByPath.has(candidatePath.path)) {
			continue;
		}

		const authority = classifySourceEvidenceAuthority(
			candidatePath.path
		);

		const target: SourceEvidenceTarget = {
			sourcePath: candidatePath.path,
			role: 'primary-source',
			authority,
			symbol: candidatePath.symbol,
			symbolAssociation: candidatePath.symbolAssociation,
			matchedTerms: questionTerms,
			targetRelevanceScore: targetScore,
		};

		sourceTargetsByPath.set(candidatePath.path, target);
		primaryTargetScores.set(candidatePath.path, targetScore);
		readableSourceTargets.push({
			...target,
		});
		readableSourceContents.set(
			candidatePath.path,
			verified.contents
		);
	}

	for (const primaryFile of readableSourceTargets.filter(
		(file) => file.role === 'primary-source'
	)) {
		const edges = findOutgoingDependencyEdges(
			params.dependencyMap,
			primaryFile.sourcePath,
			{
				includeInferred: true,
				includeUnresolved: true,
			}
		);

		for (const edge of edges) {
			if (
				edge.resolution === 'ambiguous'
				|| edge.resolution === 'unresolved'
			) {
				addWarning({
					level: 'failure',
					code: 'dependency_unresolved',
					message:
						`${primaryFile.sourcePath}: ${edge.evidence
							.map((item) => item.detail)
							.join(' ')}`,
					path: primaryFile.sourcePath,
					primarySourcePath: primaryFile.sourcePath,
				});
			}
		}

		for (const edge of edges) {
			if (
				edge.resolution === 'ambiguous'
				|| edge.resolution === 'unresolved'
				|| !edge.targetPath
			) {
				continue;
			}

			const dependencyPath =
				normalizePathForMarkdown(
					edge.targetPath
				);
			const evidenceTermsScore = countQuestionTermMatches(
				edge.evidence
					.map((item) => item.detail)
					.join(' '),
				questionTerms
			);
			const dependencyTargetScore = scoreDependencyDiscoveryTarget({
				role: 'dependency',
				authority: classifySourceEvidenceAuthority(dependencyPath),
				resolution: edge.resolution,
				primaryTargetScore:
					primaryTargetScores.get(primaryFile.sourcePath)
					?? 0,
				evidenceTermsScore,
				pathTermMatches: countTokenOverlap(
					dependencyPath,
					questionTerms
				),
			});

			const existingDependencyTarget = sourceTargetsByPath.get(
				dependencyPath
			);
			if (
				existingDependencyTarget?.role === 'dependency'
				&& (existingDependencyTarget.targetRelevanceScore ?? 0)
					>= dependencyTargetScore
			) {
				continue;
			}

			if (
				existingDependencyTarget
				&& existingDependencyTarget.role === 'primary-source'
			) {
				continue;
			}

			const dependencyTarget: SourceEvidenceTarget = {
				sourcePath: dependencyPath,
				role: 'dependency',
				authority: classifySourceEvidenceAuthority(dependencyPath),
				symbol: undefined,
				symbolAssociation: undefined,
				primarySourcePath: primaryFile.sourcePath,
				relationshipKind: edge.kind,
				resolution: edge.resolution,
				matchedTerms: questionTerms,
				targetRelevanceScore: dependencyTargetScore,
			};

			sourceTargetsByPath.set(
				dependencyPath,
				dependencyTarget
			);
			dependencyEvidenceByPath.set(
				dependencyPath,
				edge.evidence.map((item) => item.detail)
			);
		}
	}

	const dependencyTargets = [...sourceTargetsByPath.values()]
		.filter((target) => target.role === 'dependency')
		.sort((left, right) => {
			const scoreDifference =
				(right.targetRelevanceScore ?? 0)
				- (left.targetRelevanceScore ?? 0);
			if (scoreDifference !== 0) {
				return scoreDifference;
			}

			const pathDifference = left.sourcePath.localeCompare(
				right.sourcePath
			);
			if (pathDifference !== 0) {
				return pathDifference;
			}

			return (left.primarySourcePath ?? '').localeCompare(
				right.primarySourcePath ?? ''
			);
		})
		.slice(0, MAX_DISCOVERED_DEPENDENCY_SOURCE_TARGETS);

	for (const dependencyTarget of dependencyTargets) {
		if (totalDiscoveryReadBytes >= MAX_DISCOVERY_READ_BYTES) {
			break;
		}

		const verified = await readVerifiedFile({
			workspaceRoot: params.workspaceRoot,
			docsDirAbsolutePath,
			relativePath: dependencyTarget.sourcePath,
		});

		if (verified.status === 'excluded' || verified.status === 'missing') {
			addWarning({
				level: 'failure',
				code: 'dependency_unavailable',
				message:
					`${dependencyTarget.primarySourcePath ?? dependencyTarget.sourcePath}: dependency source was unavailable: ${dependencyTarget.sourcePath}`,
				path: dependencyTarget.sourcePath,
				primarySourcePath: dependencyTarget.primarySourcePath,
			});
			continue;
		}

		if (verified.status === 'unreadable') {
			addWarning({
				level: 'failure',
				code: 'dependency_unreadable',
				message:
					`${dependencyTarget.primarySourcePath ?? dependencyTarget.sourcePath}: unable to read dependency ${dependencyTarget.sourcePath}: ${verified.reason}`,
				path: dependencyTarget.sourcePath,
				primarySourcePath: dependencyTarget.primarySourcePath,
			});
			continue;
		}

		totalDiscoveryReadBytes += Buffer.byteLength(
			verified.contents,
			'utf8'
		);

		if (readableSourceContents.has(dependencyTarget.sourcePath)) {
			continue;
		}

		readableSourceTargets.push(dependencyTarget);
		readableSourceContents.set(
			dependencyTarget.sourcePath,
			verified.contents
		);
	}

	const sourceEvidenceCandidates = readableSourceTargets
		.flatMap((target) => {
			const sourceContents = readableSourceContents.get(
				target.sourcePath
			);

			if (!sourceContents) {
				return [];
			}

			if (target.symbol) {
				sourceSymbolsRequested += 1;
			}

			return buildSourceEvidenceCandidatesForFile({
				target,
				contents: sourceContents,
				questionTerms,
				onDeclarationResolution: (resolved) => {
					if (!target.symbol) {
						return;
					}

					if (resolved) {
						sourceSymbolsResolved += 1;
						return;
					}

					sourceUnresolvedSymbols += 1;
					addWarning({
						level: 'info',
						code: 'symbol_unresolved',
						message:
							`${target.sourcePath}: unable to resolve declaration for symbol ${target.symbol}; falling back to text/window extraction.`,
						path: target.sourcePath,
						primarySourcePath: target.primarySourcePath,
					});
				},
			});
		});

	for (const candidate of sourceEvidenceCandidates) {
		if (candidate.method === 'ts-declaration-span') {
			sourceDeclarationChunksGenerated += 1;
		}
	}

	const allocationResult = allocateSourceEvidenceChunks({
		candidates: sourceEvidenceCandidates,
		options: {
			totalBudgetChars: MAX_VERIFIED_TOTAL_CHARS,
			perFileCeilingChars: MAX_VERIFIED_PER_FILE_CHARS,
			perChunkCapChars: MAX_VERIFIED_FILE_CHARS,
		},
	});

	for (const chunk of allocationResult.chunks) {
		if (chunk.method === 'ts-declaration-span') {
			sourceDeclarationChunksIncluded += 1;
		}

		files.push({
			path: chunk.sourcePath,
			role: chunk.role,
			authority: chunk.authority,
			primarySourcePath: chunk.primarySourcePath,
			relationshipKind: chunk.relationshipKind,
			resolution: chunk.resolution,
			evidence:
				chunk.role === 'dependency'
					? dependencyEvidenceByPath.get(
						chunk.sourcePath
					)
					: undefined,
			symbol: chunk.symbol,
			startLine: chunk.startLine,
			endLine: chunk.endLine,
			extractionMethod: chunk.method,
			includedChars: chunk.includedChars,
			clippedByBudget: chunk.clippedByBudget,
			contents: chunk.contents,
		});

		if (chunk.clippedByBudget) {
			addWarning({
				level: 'info',
				code:
					chunk.role === 'primary-source'
						? 'source_clipped'
						: 'dependency_clipped',
				message:
					`${chunk.sourcePath}: verified ${chunk.role === 'primary-source' ? 'source' : 'dependency source'} excerpt lines ${chunk.startLine}-${chunk.endLine} was clipped by source evidence budget limits.`,
				path: chunk.sourcePath,
				primarySourcePath: chunk.primarySourcePath,
			});
		}
	}

	const uniqueSourcePaths = new Set(
		allocationResult.chunks.map((chunk) => chunk.sourcePath)
	);
	const includedPaths = new Set(
		allocationResult.chunks.map((chunk) => chunk.sourcePath)
	);
	const topRankedTargets = [...sourceTargetsByPath.values()]
		.sort((left, right) => {
			const scoreDifference =
				(right.targetRelevanceScore ?? 0)
				- (left.targetRelevanceScore ?? 0);
			if (scoreDifference !== 0) {
				return scoreDifference;
			}

			return left.sourcePath.localeCompare(right.sourcePath);
		})
		.slice(0, MAX_TOP_RANKED_TARGETS)
		.map((target) => ({
			path: target.sourcePath,
			targetScore: target.targetRelevanceScore ?? 0,
			authority: target.authority,
			symbol: target.symbol,
			method: allocationResult.chunks.find(
				(chunk) => chunk.sourcePath === target.sourcePath
			)?.method,
			included: includedPaths.has(target.sourcePath),
		}));

	return {
		files,
		warnings: warningDetails.map((warning) => warning.message),
		warningDetails,
		sourceTargetsDiscovered: sourceTargetsByPath.size,
		sourceFilesRead: readableSourceContents.size,
		sourceFilesConsidered: readableSourceTargets.length,
		sourceChunkCandidates: sourceEvidenceCandidates.length,
		sourceChunksIncluded: allocationResult.chunks.length,
		verifiedSourceCharactersIncluded:
			allocationResult.totalIncludedChars,
		sourceChunksClippedByBudget:
			allocationResult.chunksClippedByBudget,
		wholeFileFallbackChunks:
			allocationResult.wholeFileFallbackChunks,
		verifiedSourceChunkCount:
			allocationResult.chunks.length,
		verifiedSourceUniqueFileCount:
			uniqueSourcePaths.size,
		sourceSymbolsRequested,
		sourceSymbolsResolved,
		sourceDeclarationChunksGenerated,
		sourceDeclarationChunksIncluded,
		sourceUnresolvedSymbols,
		topRankedTargets,
	};
}
