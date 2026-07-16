import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	type DependencyMap,
	findOutgoingDependencyEdges,
} from './dependencyMap';
import {
	isPathInsideDirectory,
} from './sourceDiscovery';
import type {
	ResolvedSummarizationInstructions,
} from './summarizationConfig';
import {
	normalizePathForMarkdown,
} from './workspace';

export interface SummarizationDependencyContextFile {
	primarySourcePath: string;
	path: string;
	relationshipKind: string;
	resolution: 'exact' | 'inferred';
	evidence: string[];
	contents: string;
}

export interface SummarizationDependencyContextResult {
	files: SummarizationDependencyContextFile[];
	warnings: string[];
}

export async function hydrateSummarizationDependencyContext(
	params: {
		workspaceRoot: string;
		dependencyMap: DependencyMap;
		sourcePaths: string[];
		resolvedBySource:
			ResolvedSummarizationInstructions[];
	}
): Promise<SummarizationDependencyContextResult> {
	const files: SummarizationDependencyContextFile[] = [];
	const warnings: string[] = [];

	for (
		let sourceIndex = 0;
		sourceIndex < params.sourcePaths.length;
		sourceIndex += 1
	) {
		const primarySourcePath =
			normalizePathForMarkdown(
				params.sourcePaths[sourceIndex]
			);
		const strategy =
			params.resolvedBySource[sourceIndex]
				?.dependencyStrategy;

		if (!strategy) {
			continue;
		}

		const pending: Array<{
			sourcePath: string;
			depth: number;
			firstHop: boolean;
		}> = [
			{
				sourcePath: primarySourcePath,
				depth: 0,
				firstHop: true,
			},
		];
		const visitedTraversalPaths =
			new Set<string>();
		const includedDependencyPaths =
			new Set<string>();

		let includedFileCount = 0;
		let includedChars = 0;
		let fileLimitWarningAdded = false;
		let charLimitWarningAdded = false;

		while (pending.length > 0) {
			const current = pending.shift();

			if (!current) {
				break;
			}

			if (
				current.depth >= strategy.maxDepth
				|| visitedTraversalPaths.has(
					current.sourcePath
				)
			) {
				continue;
			}

			visitedTraversalPaths.add(
				current.sourcePath
			);

			const edges = findOutgoingDependencyEdges(
				params.dependencyMap,
				current.sourcePath,
				{
					...(current.firstHop
						? { kinds: strategy.follow }
						: {}),
					includeInferred:
						strategy.includeInferred,
					includeUnresolved: true,
				}
			);

			for (const edge of edges) {
				if (
					edge.resolution === 'ambiguous'
					|| edge.resolution === 'unresolved'
				) {
					warnings.push(
						`${current.sourcePath}: ${edge.evidence
							.map((evidence) =>
								evidence.detail
							)
							.join(' ')}`
					);
				}
			}

			for (const edge of edges) {
				if (
					edge.resolution === 'ambiguous'
					|| edge.resolution === 'unresolved'
				) {
					continue;
				}

				if (!edge.targetPath) {
					warnings.push(
						`${current.sourcePath}: ${edge.kind} edge has no target path.`
					);
					continue;
				}

				const dependencyPath =
					normalizePathForMarkdown(
						edge.targetPath
					);
				const dependencyDepth =
					current.depth + 1;

				if (
					includedDependencyPaths.has(
						dependencyPath
					)
				) {
					continue;
				}

				if (
					includedFileCount
						>= strategy.maxFiles
				) {
					if (!fileLimitWarningAdded) {
						warnings.push(
							`${primarySourcePath}: dependency context was limited to ${strategy.maxFiles} file(s).`
						);
						fileLimitWarningAdded = true;
					}
					break;
				}

				const remainingChars =
					strategy.maxChars
					- includedChars;

				if (remainingChars <= 0) {
					if (!charLimitWarningAdded) {
						warnings.push(
							`${primarySourcePath}: dependency context reached the ${strategy.maxChars}-character limit.`
						);
						charLimitWarningAdded = true;
					}
					break;
				}

				const dependencyAbsolutePath =
					path.resolve(
						params.workspaceRoot,
						dependencyPath
					);

				if (
					!isPathInsideDirectory(
						dependencyAbsolutePath,
						params.workspaceRoot
					)
				) {
					warnings.push(
						`${current.sourcePath}: dependency target is outside the workspace: ${dependencyPath}`
					);
					continue;
				}

				let contents: string;

				try {
					contents = await fs.readFile(
						dependencyAbsolutePath,
						'utf8'
					);
				} catch (error) {
					const message =
						error instanceof Error
							? error.message
							: String(error);

					warnings.push(
						`${current.sourcePath}: unable to read dependency ${dependencyPath}: ${message}`
					);
					continue;
				}

				const clippedContents =
					contents.slice(
						0,
						remainingChars
					);

				if (
					clippedContents.length
						< contents.length
				) {
					warnings.push(
						`${primarySourcePath}: dependency ${dependencyPath} was clipped to the remaining ${remainingChars} characters.`
					);
				}

				files.push({
					primarySourcePath,
					path: dependencyPath,
					relationshipKind: edge.kind,
					resolution: edge.resolution,
					evidence: edge.evidence.map(
						(evidence) =>
							evidence.detail
					),
					contents: clippedContents,
				});

				includedDependencyPaths.add(
					dependencyPath
				);
				includedFileCount += 1;
				includedChars +=
					clippedContents.length;

				if (
					dependencyDepth
						< strategy.maxDepth
				) {
					pending.push({
						sourcePath:
							dependencyPath,
						depth:
							dependencyDepth,
						firstHop: false,
					});
				}
			}
		}
	}

	return {
		files,
		warnings: [...new Set(warnings)],
	};
}
