import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	type DependencyMap,
	findOutgoingDependencyEdges,
} from './dependencyMap';
import {
	isPathInsideDirectory,
} from './sourceDiscovery';
import {
	normalizePathForMarkdown,
} from './workspace';

const MAX_VERIFIED_PRIMARY_SOURCE_FILES = 8;
const MAX_VERIFIED_DEPENDENCY_FILES = 8;
const MAX_VERIFIED_FILE_CHARS = 12000;
const MAX_VERIFIED_TOTAL_CHARS = 60000;

export interface AnswerSummaryEvidence {
	path: string;
	contents: string;
}

export interface VerifiedAnswerSourceFile {
	path: string;
	role: 'primary-source' | 'dependency';
	primarySourcePath?: string;
	relationshipKind?: string;
	resolution?: 'exact' | 'inferred';
	evidence?: string[];
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
	| 'dependency_unreadable';

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
	files: VerifiedAnswerSourceFile[];
	warnings: string[];
	warningDetails: VerifiedAnswerSourceWarning[];
}

interface ExtractedVerificationCandidate {
	path: string;
	markedAsSourcePath: boolean;
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

function extractVerificationCandidates(
	contents: string
): ExtractedVerificationCandidate[] {
	const candidates = new Map<string, ExtractedVerificationCandidate>();
	const regex = /`([^`\r\n]+)`/g;
	let match: RegExpExecArray | null;
	const upsertCandidate = (
		rawCandidate: string,
		markedAsSourcePath: boolean
	): void => {
		const normalized = normalizeVerificationCandidatePath(rawCandidate.trim());

		if (!normalized) {
			return;
		}

		const existing = candidates.get(normalized);

		if (existing) {
			existing.markedAsSourcePath =
				existing.markedAsSourcePath || markedAsSourcePath;
			return;
		}

		candidates.set(normalized, {
			path: normalized,
			markedAsSourcePath,
		});
	};

	while ((match = regex.exec(contents)) !== null) {
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

		upsertCandidate(candidate, false);
	}

	const structuredPathRegex =
		/(?:"(?:sourcePath|source_path|sourceFile|source_file)"\s*:\s*"([^"\r\n]+)")|(?:\b(?:sourcePath|source_path|sourceFile|source_file)\s*:\s*([^\s#`][^\r\n`]*))/g;
	let structuredMatch: RegExpExecArray | null;

	while ((structuredMatch = structuredPathRegex.exec(contents)) !== null) {
		const metadataPath =
			structuredMatch[1]
			?? structuredMatch[2]
			?? '';

		if (!metadataPath) {
			continue;
		}

		upsertCandidate(metadataPath, true);
	}

	return [...candidates.values()];
}

async function readVerifiedFile(params: {
	workspaceRoot: string;
	docsDirAbsolutePath: string;
	relativePath: string;
	remainingChars: number;
}): Promise<
	| {
		status: 'ok';
		contents: string;
		clipped: boolean;
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

	const limit = Math.min(
		MAX_VERIFIED_FILE_CHARS,
		params.remainingChars
	);
	const clippedContents = contents.slice(0, limit);

	return {
		status: 'ok',
		contents: clippedContents,
		clipped:
			clippedContents.length < contents.length,
	};
}

export async function collectVerifiedAnswerSourceContext(
	params: {
		workspaceRoot: string;
		docsDir: string;
		summaryEvidence: AnswerSummaryEvidence[];
		dependencyMap: DependencyMap;
	}
): Promise<VerifiedAnswerSourceContext> {
	const docsDirAbsolutePath = path.resolve(
		params.workspaceRoot,
		params.docsDir
	);
	const files: VerifiedAnswerSourceFile[] = [];
	const warningDetails: VerifiedAnswerSourceWarning[] = [];
	const warningDetailKeys = new Set<string>();
	const includedPaths = new Set<string>();
	let totalChars = 0;

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
		for (const candidate of extractVerificationCandidates(evidence.contents)) {
			const existingCandidate = candidatePathMap.get(candidate.path);

			if (existingCandidate) {
				existingCandidate.markedAsSourcePath =
					existingCandidate.markedAsSourcePath
					|| candidate.markedAsSourcePath;
				continue;
			}

			candidatePathMap.set(candidate.path, candidate);
		}
	}

	const candidatePaths = [...candidatePathMap.values()];

	let primaryFileCount = 0;

	for (const candidatePath of candidatePaths) {
		if (
			!isFileVerificationCandidate({
				candidate: candidatePath.path,
				markedAsSourcePath: candidatePath.markedAsSourcePath,
			})
		) {
			continue;
		}

		if (
			primaryFileCount
				>= MAX_VERIFIED_PRIMARY_SOURCE_FILES
			|| totalChars >= MAX_VERIFIED_TOTAL_CHARS
		) {
			break;
		}

		const remainingChars =
			MAX_VERIFIED_TOTAL_CHARS - totalChars;

		const verified = await readVerifiedFile({
			workspaceRoot: params.workspaceRoot,
			docsDirAbsolutePath,
			relativePath: candidatePath.path,
			remainingChars,
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

		files.push({
			path: candidatePath.path,
			role: 'primary-source',
			contents: verified.contents,
		});
		includedPaths.add(candidatePath.path);
		primaryFileCount += 1;
		totalChars += verified.contents.length;

		if (verified.clipped) {
			addWarning({
				level: 'info',
				code: 'source_clipped',
				message:
					`${candidatePath.path}: verified source was clipped to ${MAX_VERIFIED_FILE_CHARS} characters.`,
				path: candidatePath.path,
			});
		}
	}

	let dependencyFileCount = 0;

	for (const primaryFile of files.filter(
		(file) => file.role === 'primary-source'
	)) {
		const edges = findOutgoingDependencyEdges(
			params.dependencyMap,
			primaryFile.path,
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
						`${primaryFile.path}: ${edge.evidence
							.map((item) => item.detail)
							.join(' ')}`,
					path: primaryFile.path,
					primarySourcePath: primaryFile.path,
				});
			}
		}

		for (const edge of edges) {
			if (
				dependencyFileCount
					>= MAX_VERIFIED_DEPENDENCY_FILES
				|| totalChars
					>= MAX_VERIFIED_TOTAL_CHARS
			) {
				break;
			}

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

			if (includedPaths.has(dependencyPath)) {
				continue;
			}

			const remainingChars =
				MAX_VERIFIED_TOTAL_CHARS - totalChars;

			const verified = await readVerifiedFile({
				workspaceRoot: params.workspaceRoot,
				docsDirAbsolutePath,
				relativePath: dependencyPath,
				remainingChars,
			});

			if (verified.status === 'excluded' || verified.status === 'missing') {
				addWarning({
					level: 'failure',
					code: 'dependency_unavailable',
					message:
						`${primaryFile.path}: dependency source was unavailable: ${dependencyPath}`,
					path: dependencyPath,
					primarySourcePath: primaryFile.path,
				});
				continue;
			}

			if (verified.status === 'unreadable') {
				addWarning({
					level: 'failure',
					code: 'dependency_unreadable',
					message:
						`${primaryFile.path}: unable to read dependency ${dependencyPath}: ${verified.reason}`,
					path: dependencyPath,
					primarySourcePath: primaryFile.path,
				});
				continue;
			}

			files.push({
				path: dependencyPath,
				role: 'dependency',
				primarySourcePath:
					primaryFile.path,
				relationshipKind: edge.kind,
				resolution: edge.resolution,
				evidence: edge.evidence.map(
					(item) => item.detail
				),
				contents: verified.contents,
			});
			includedPaths.add(dependencyPath);
			dependencyFileCount += 1;
			totalChars += verified.contents.length;

			if (verified.clipped) {
				addWarning({
					level: 'info',
					code: 'dependency_clipped',
					message:
						`${dependencyPath}: verified dependency source was clipped to ${MAX_VERIFIED_FILE_CHARS} characters.`,
					path: dependencyPath,
					primarySourcePath: primaryFile.path,
				});
			}
		}
	}

	return {
		files,
		warnings: warningDetails.map((warning) => warning.message),
		warningDetails,
	};
}
