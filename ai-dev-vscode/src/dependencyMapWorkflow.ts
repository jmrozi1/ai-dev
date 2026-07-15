import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	type DependencyEdge,
	getDependencyMapPath,
	readDependencyMap,
	sortDependencyEdges,
	writeDependencyMap,
} from './dependencyMap';
import {
	JENKINS_PIPELINE_SCRIPT_EDGE_KIND,
	resolveJenkinsPipelineDependency,
} from './jenkinsDependencyResolver';
import {
	discoverBatchUnitDocCandidates,
} from './sourceDiscovery';
import {
	readAiDevConfig,
} from './config';
import {
	normalizePathForMarkdown,
} from './workspace';

export interface DependencyMapRefreshResult {
	dependencyMapPath: string;
	scannedConfigCount: number;
	resolvedEdgeCount: number;
	exactCount: number;
	inferredCount: number;
	ambiguousCount: number;
	unresolvedCount: number;
	skippedCount: number;
	failedCount: number;
	warnings: string[];
}

function isJenkinsConfigPath(
	relativePath: string
): boolean {
	return path.posix.basename(
		normalizePathForMarkdown(relativePath)
	).toLowerCase() === 'config.xml';
}

export async function refreshJenkinsDependencyMap(
	workspaceRoot: string
): Promise<DependencyMapRefreshResult> {
	const config = await readAiDevConfig(workspaceRoot);
	const candidateAbsolutePaths =
		await discoverBatchUnitDocCandidates(
			workspaceRoot,
			config
		);

	const candidatePaths = candidateAbsolutePaths.map(
		(absolutePath) =>
			normalizePathForMarkdown(
				path.relative(workspaceRoot, absolutePath)
			)
	);

	const configCandidates = candidateAbsolutePaths
		.map((absolutePath, index) => ({
			absolutePath,
			relativePath: candidatePaths[index],
		}))
		.filter((candidate) =>
			isJenkinsConfigPath(candidate.relativePath)
		)
		.sort((left, right) =>
			left.relativePath.localeCompare(
				right.relativePath
			)
		);

	const existingMap =
		await readDependencyMap(workspaceRoot, config);
	const scannedConfigPaths = new Set<string>();
	const generatedEdges: DependencyEdge[] = [];
	const warnings: string[] = [];

	let skippedCount = 0;
	let failedCount = 0;

	for (const candidate of configCandidates) {
		let configXml: string;

		try {
			configXml = await fs.readFile(
				candidate.absolutePath,
				'utf8'
			);
		} catch (error) {
			failedCount += 1;
			const message =
				error instanceof Error
					? error.message
					: String(error);

			warnings.push(
				`${candidate.relativePath}: failed to read Jenkins config: ${message}`
			);
			continue;
		}

		scannedConfigPaths.add(candidate.relativePath);

		const edge = resolveJenkinsPipelineDependency({
			configPath: candidate.relativePath,
			configXml,
			candidatePaths,
		});

		if (edge) {
			generatedEdges.push(edge);
		} else {
			skippedCount += 1;
		}
	}

	const preservedEdges = existingMap.edges.filter(
		(edge) =>
			!(
				edge.kind
					=== JENKINS_PIPELINE_SCRIPT_EDGE_KIND
				&& scannedConfigPaths.has(
					normalizePathForMarkdown(
						edge.sourcePath
					)
				)
			)
	);

	const mergedEdges = sortDependencyEdges([
		...preservedEdges,
		...generatedEdges,
	]);

	await writeDependencyMap(
		workspaceRoot,
		config,
		{
			version: 1,
			edges: mergedEdges,
		}
	);

	const countResolution = (
		resolution:
			| 'exact'
			| 'inferred'
			| 'ambiguous'
			| 'unresolved'
	): number =>
		generatedEdges.filter(
			(edge) => edge.resolution === resolution
		).length;

	for (const edge of generatedEdges) {
		if (
			edge.resolution === 'ambiguous'
			|| edge.resolution === 'unresolved'
		) {
			warnings.push(
				`${edge.sourcePath}: ${edge.evidence
					.map((evidence) => evidence.detail)
					.join(' ')}`
			);
		}
	}

	return {
		dependencyMapPath:
			getDependencyMapPath(config),
		scannedConfigCount:
			scannedConfigPaths.size,
		resolvedEdgeCount:
			generatedEdges.length,
		exactCount: countResolution('exact'),
		inferredCount:
			countResolution('inferred'),
		ambiguousCount:
			countResolution('ambiguous'),
		unresolvedCount:
			countResolution('unresolved'),
		skippedCount,
		failedCount,
		warnings,
	};
}
