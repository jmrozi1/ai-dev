import {
	NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS,
	globToRegExp,
	matchesAnyGlob,
} from './pathMatching';
import {
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
