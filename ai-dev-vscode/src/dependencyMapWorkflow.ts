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
	SHELL_COMMAND_SCRIPT_EDGE_KIND,
	SHELL_SOURCE_EDGE_KIND,
	resolveDelegatedFileDependencies,
} from './delegatedFileDependencyResolver';
import {
	discoverBatchUnitDocCandidates,
	parseYamlList,
} from './sourceDiscovery';
import {
	readAiDevConfig,
} from './config';
import {
	matchesAnyGlob,
} from './pathMatching';
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

export interface DependencyMapPreflightResult {
	refreshed: boolean;
	warnings: string[];
}

export interface JenkinsDependencyResolverScope {
	includeGlobs: string[];
	excludeGlobs: string[];
}

export function getJenkinsDependencyResolverScope(
	config: { raw: string }
): JenkinsDependencyResolverScope {
	return {
		includeGlobs: parseYamlList(
			config.raw,
			'dependencyResolvers',
			'jenkinsConfigInclude'
		),
		excludeGlobs: parseYamlList(
			config.raw,
			'dependencyResolvers',
			'jenkinsConfigExclude'
		),
	};
}

export function isJenkinsConfigInResolverScope(
	relativePath: string,
	scope: JenkinsDependencyResolverScope
): boolean {
	const normalizedPath =
		normalizePathForMarkdown(relativePath);

	if (!isJenkinsConfigPath(normalizedPath)) {
		return false;
	}

	if (
		scope.includeGlobs.length > 0
		&& !matchesAnyGlob(
			normalizedPath,
			scope.includeGlobs
		)
	) {
		return false;
	}

	if (
		scope.excludeGlobs.length > 0
		&& matchesAnyGlob(
			normalizedPath,
			scope.excludeGlobs
		)
	) {
		return false;
	}

	return true;
}

export function isJenkinsConfigPath(
	relativePath: string
): boolean {
	return path.posix.basename(
		normalizePathForMarkdown(relativePath)
	).toLowerCase() === 'config.xml';
}

export function shouldRefreshJenkinsDependencyMap(
	sourcePaths: string[]
): boolean {
	return sourcePaths.some((sourcePath) =>
		isJenkinsConfigPath(sourcePath)
	);
}

export async function refreshJenkinsDependencyMap(
	workspaceRoot: string
): Promise<DependencyMapRefreshResult> {
	const config = await readAiDevConfig(workspaceRoot);
	const resolverScope =
		getJenkinsDependencyResolverScope(config);
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
			isJenkinsConfigInResolverScope(
				candidate.relativePath,
				resolverScope
			)
		)
		.sort((left, right) =>
			left.relativePath.localeCompare(
				right.relativePath
			)
		);

	const existingMap =
		await readDependencyMap(workspaceRoot, config);
	const scannedConfigPaths = new Set<string>();
	const failedConfigPaths = new Set<string>();
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
			failedConfigPaths.add(
				candidate.relativePath
			);
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

	const delegatedEdgeKinds = new Set([
		SHELL_COMMAND_SCRIPT_EDGE_KIND,
		SHELL_SOURCE_EDGE_KIND,
	]);
	const candidateByPath = new Map(
		candidatePaths.map((relativePath, index) => [
			relativePath,
			candidateAbsolutePaths[index],
		])
	);
	const pendingDelegatedFiles: Array<{
		relativePath: string;
		depth: number;
	}> = generatedEdges
		.filter(
			(edge) =>
				(edge.resolution === 'exact'
					|| edge.resolution === 'inferred')
				&& edge.targetPath
		)
		.map((edge) => ({
			relativePath:
				normalizePathForMarkdown(
					edge.targetPath as string
				),
			depth: 1,
		}));
	const visitedDelegatedPaths =
		new Set<string>();
	const maxDelegatedDepth = 5;
	const maxDelegatedFiles = 100;
	let delegatedFileCount = 0;

	while (
		pendingDelegatedFiles.length > 0
		&& delegatedFileCount < maxDelegatedFiles
	) {
		const current =
			pendingDelegatedFiles.shift();

		if (
			!current
			|| current.depth > maxDelegatedDepth
			|| visitedDelegatedPaths.has(
				current.relativePath
			)
		) {
			continue;
		}

		visitedDelegatedPaths.add(
			current.relativePath
		);

		const absolutePath =
			candidateByPath.get(
				current.relativePath
			);

		if (!absolutePath) {
			continue;
		}

		let sourceContents: string;

		try {
			sourceContents = await fs.readFile(
				absolutePath,
				'utf8'
			);
		} catch (error) {
			const message =
				error instanceof Error
					? error.message
					: String(error);

			warnings.push(
				`${current.relativePath}: failed to read delegated dependency source: ${message}`
			);
			continue;
		}

		delegatedFileCount += 1;

		const delegatedEdges =
			resolveDelegatedFileDependencies({
				sourcePath:
					current.relativePath,
				sourceContents,
				candidatePaths,
			});

		generatedEdges.push(
			...delegatedEdges
		);

		for (const edge of delegatedEdges) {
			if (
				(edge.resolution !== 'exact'
					&& edge.resolution
						!== 'inferred')
				|| !edge.targetPath
			) {
				continue;
			}

			pendingDelegatedFiles.push({
				relativePath:
					normalizePathForMarkdown(
						edge.targetPath
					),
				depth: current.depth + 1,
			});
		}
	}

	const preservedEdges = existingMap.edges.filter(
		(edge) => {
			if (
				edge.kind
					!== JENKINS_PIPELINE_SCRIPT_EDGE_KIND
				&& !delegatedEdgeKinds.has(
					edge.kind
				)
			) {
				return true;
			}

			if (delegatedEdgeKinds.has(edge.kind)) {
				return false;
			}

			return failedConfigPaths.has(
				normalizePathForMarkdown(
					edge.sourcePath
				)
			);
		}
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


export async function refreshJenkinsDependencyMapForSummarization(
	workspaceRoot: string,
	sourcePaths: string[],
	refresh: (
		workspaceRoot: string
	) => Promise<DependencyMapRefreshResult> =
		refreshJenkinsDependencyMap
): Promise<DependencyMapPreflightResult> {
	if (
		!shouldRefreshJenkinsDependencyMap(
			sourcePaths
		)
	) {
		return {
			refreshed: false,
			warnings: [],
		};
	}

	try {
		const result = await refresh(workspaceRoot);

		return {
			refreshed: true,
			warnings: result.warnings,
		};
	} catch (error) {
		const message =
			error instanceof Error
				? error.message
				: String(error);

		return {
			refreshed: false,
			warnings: [
				`Dependency map refresh failed: ${message}`,
			],
		};
	}
}
