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

export interface VerifiedAnswerSourceContext {
	files: VerifiedAnswerSourceFile[];
	warnings: string[];
}

function extractBacktickedPathCandidates(
	contents: string
): string[] {
	const candidates: string[] = [];
	const regex = /`([^`\r\n]+)`/g;
	let match: RegExpExecArray | null;

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

		candidates.push(
			normalizePathForMarkdown(candidate)
				.replace(/^\.\/+/, '')
				.replace(/^\/+/, '')
		);
	}

	return [...new Set(candidates)];
}

async function readVerifiedFile(params: {
	workspaceRoot: string;
	docsDirAbsolutePath: string;
	relativePath: string;
	remainingChars: number;
}): Promise<
	| {
		contents: string;
		clipped: boolean;
	}
	| undefined
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
		return undefined;
	}

	let stat;

	try {
		stat = await fs.stat(absolutePath);
	} catch {
		return undefined;
	}

	if (!stat.isFile()) {
		return undefined;
	}

	const contents = await fs.readFile(
		absolutePath,
		'utf8'
	);
	const limit = Math.min(
		MAX_VERIFIED_FILE_CHARS,
		params.remainingChars
	);
	const clippedContents = contents.slice(0, limit);

	return {
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
	const warnings: string[] = [];
	const includedPaths = new Set<string>();
	let totalChars = 0;

	const candidatePaths = [
		...new Set(
			params.summaryEvidence.flatMap(
				(evidence) =>
					extractBacktickedPathCandidates(
						evidence.contents
					)
			)
		),
	];

	let primaryFileCount = 0;

	for (const candidatePath of candidatePaths) {
		if (
			primaryFileCount
				>= MAX_VERIFIED_PRIMARY_SOURCE_FILES
			|| totalChars >= MAX_VERIFIED_TOTAL_CHARS
		) {
			break;
		}

		const remainingChars =
			MAX_VERIFIED_TOTAL_CHARS - totalChars;

		let verified;

		try {
			verified = await readVerifiedFile({
				workspaceRoot: params.workspaceRoot,
				docsDirAbsolutePath,
				relativePath: candidatePath,
				remainingChars,
			});
		} catch (error) {
			warnings.push(
				`${candidatePath}: unable to read source for answer verification: ${
					error instanceof Error
						? error.message
						: String(error)
				}`
			);
			continue;
		}

		if (!verified) {
			continue;
		}

		files.push({
			path: candidatePath,
			role: 'primary-source',
			contents: verified.contents,
		});
		includedPaths.add(candidatePath);
		primaryFileCount += 1;
		totalChars += verified.contents.length;

		if (verified.clipped) {
			warnings.push(
				`${candidatePath}: verified source was clipped to ${MAX_VERIFIED_FILE_CHARS} characters.`
			);
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
				warnings.push(
					`${primaryFile.path}: ${edge.evidence
						.map((item) => item.detail)
						.join(' ')}`
				);
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

			let verified;

			try {
				verified = await readVerifiedFile({
					workspaceRoot: params.workspaceRoot,
					docsDirAbsolutePath,
					relativePath: dependencyPath,
					remainingChars,
				});
			} catch (error) {
				warnings.push(
					`${primaryFile.path}: unable to read dependency ${dependencyPath}: ${
						error instanceof Error
							? error.message
							: String(error)
					}`
				);
				continue;
			}

			if (!verified) {
				warnings.push(
					`${primaryFile.path}: dependency source was unavailable: ${dependencyPath}`
				);
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
				warnings.push(
					`${dependencyPath}: verified dependency source was clipped to ${MAX_VERIFIED_FILE_CHARS} characters.`
				);
			}
		}
	}

	return {
		files,
		warnings: [...new Set(warnings)],
	};
}
