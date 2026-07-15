import * as path from 'node:path';

const NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS = [
	'**/*.vsix',
	'**/*.zip',
	'**/*.tar',
	'**/*.tar.gz',
	'**/*.tgz',
	'**/*.jar',
	'**/*.war',
	'**/*.ear',
	'**/*.dll',
	'**/*.exe',
	'**/*.bin',
	'**/*.class',
	'**/*.pyc',
	'**/*.pyo',
	'**/*.so',
	'**/*.dylib',
	'**/*.a',
	'**/*.lib',
	'**/*.o',
	'**/*.obj',
];

function normalizePathForReview(filePath: string): string {
	return filePath
		.replaceAll(path.sep, '/')
		.replace(/^\.\//, '');
}

function globToRegExp(glob: string): RegExp {
	const normalized = normalizePathForReview(glob);
	let expression = '^';

	for (let index = 0; index < normalized.length; index += 1) {
		const character = normalized[index];

		if (character === '*') {
			if (normalized[index + 1] === '*') {
				const followedBySlash =
					normalized[index + 2] === '/';

				expression += followedBySlash
					? '(?:.*/)?'
					: '.*';

				index += followedBySlash ? 2 : 1;
			} else {
				expression += '[^/]*';
			}

			continue;
		}

		if (character === '?') {
			expression += '[^/]';
			continue;
		}

		if (character === '[') {
			const closingIndex =
				normalized.indexOf(']', index + 1);

			if (closingIndex >= 0) {
				expression += normalized.slice(
					index,
					closingIndex + 1
				);
				index = closingIndex;
				continue;
			}
		}

		if (character === '{') {
			const closingIndex =
				normalized.indexOf('}', index + 1);

			if (closingIndex >= 0) {
				const alternatives = normalized
					.slice(index + 1, closingIndex)
					.split(',')
					.map((value) =>
						value.replace(
							/[.*+?^${}()|[\]\\]/g,
							'\\$&'
						)
					);

				expression += `(?:${alternatives.join('|')})`;
				index = closingIndex;
				continue;
			}
		}

		expression += character.replace(
			/[.*+?^${}()|[\]\\]/g,
			'\\$&'
		);
	}

	return new RegExp(`${expression}$`);
}

function matchesAnyGlob(
	filePath: string,
	globs: string[]
): boolean {
	return globs.some((glob) =>
		globToRegExp(glob).test(filePath)
	);
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
