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
	const includedEdgeKeys = new Set<string>();

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

		const edges = findOutgoingDependencyEdges(
			params.dependencyMap,
			primarySourcePath,
			{
				kinds: strategy.follow,
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
					`${primarySourcePath}: ${edge.evidence
						.map((evidence) =>
							evidence.detail
						)
						.join(' ')}`
				);
			}
		}

		let includedFileCount = 0;
		let includedChars = 0;

		for (const edge of edges) {
			if (
				edge.resolution === 'ambiguous'
				|| edge.resolution === 'unresolved'
			) {
				continue;
			}

			if (!edge.targetPath) {
				warnings.push(
					`${primarySourcePath}: ${edge.kind} edge has no target path.`
				);
				continue;
			}

			const dependencyPath =
				normalizePathForMarkdown(
					edge.targetPath
				);
			const edgeKey = [
				primarySourcePath,
				dependencyPath,
				edge.kind,
			].join('\0');

			if (includedEdgeKeys.has(edgeKey)) {
				continue;
			}

			if (
				includedFileCount
				>= strategy.maxFiles
			) {
				warnings.push(
					`${primarySourcePath}: dependency context was limited to ${strategy.maxFiles} file(s).`
				);
				break;
			}

			const remainingChars =
				strategy.maxChars - includedChars;

			if (remainingChars <= 0) {
				warnings.push(
					`${primarySourcePath}: dependency context reached the ${strategy.maxChars}-character limit.`
				);
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
					`${primarySourcePath}: dependency target is outside the workspace: ${dependencyPath}`
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
					`${primarySourcePath}: unable to read dependency ${dependencyPath}: ${message}`
				);
				continue;
			}

			const clippedContents =
				contents.slice(0, remainingChars);

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
					(evidence) => evidence.detail
				),
				contents: clippedContents,
			});

			includedEdgeKeys.add(edgeKey);
			includedFileCount += 1;
			includedChars += clippedContents.length;
		}
	}

	return {
		files,
		warnings: [...new Set(warnings)],
	};
}
