import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import type { AiDevConfig } from './config';
import {
	getConfiguredDocsDir,
} from './sourceDiscovery';
import {
	normalizePathForMarkdown,
} from './workspace';

export const DEPENDENCY_MAP_FILE_NAME =
	'dependency-map.json';

export type DependencyResolution =
	| 'exact'
	| 'inferred'
	| 'ambiguous'
	| 'unresolved';

export interface DependencyEvidence {
	kind: string;
	detail: string;
	sourcePath?: string;
}

export interface DependencyEdge {
	sourcePath: string;
	targetPath?: string;
	kind: string;
	resolution: DependencyResolution;
	evidence: DependencyEvidence[];
}

export interface DependencyMap {
	version: 1;
	edges: DependencyEdge[];
}

export interface DependencyMapValidationIssue {
	field: string;
	message: string;
	edgeIndex?: number;
}

const RESOLUTION_RANK: Record<
	DependencyResolution,
	number
> = {
	exact: 0,
	inferred: 1,
	ambiguous: 2,
	unresolved: 3,
};

function isDependencyResolution(
	value: unknown
): value is DependencyResolution {
	return (
		value === 'exact'
		|| value === 'inferred'
		|| value === 'ambiguous'
		|| value === 'unresolved'
	);
}

function normalizeOptionalPath(
	value: unknown
): string | undefined {
	if (typeof value !== 'string') {
		return undefined;
	}

	const trimmed = value.trim();
	return trimmed
		? normalizePathForMarkdown(trimmed)
		: undefined;
}

function normalizeEvidence(
	value: unknown
): DependencyEvidence {
	const raw =
		value && typeof value === 'object'
			? value as Partial<DependencyEvidence>
			: {};

	return {
		kind:
			typeof raw.kind === 'string'
				? raw.kind.trim()
				: '',
		detail:
			typeof raw.detail === 'string'
				? raw.detail.trim()
				: '',
		sourcePath:
			normalizeOptionalPath(raw.sourcePath),
	};
}

function normalizeEdge(
	value: unknown
): DependencyEdge {
	const raw =
		value && typeof value === 'object'
			? value as Partial<DependencyEdge>
			: {};

	return {
		sourcePath:
			normalizeOptionalPath(raw.sourcePath) ?? '',
		targetPath:
			normalizeOptionalPath(raw.targetPath),
		kind:
			typeof raw.kind === 'string'
				? raw.kind.trim()
				: '',
		resolution:
			isDependencyResolution(raw.resolution)
				? raw.resolution
				: 'unresolved',
		evidence:
			Array.isArray(raw.evidence)
				? raw.evidence.map(normalizeEvidence)
				: [],
	};
}

export function createEmptyDependencyMap():
DependencyMap {
	return {
		version: 1,
		edges: [],
	};
}

export function sortDependencyEdges(
	edges: DependencyEdge[]
): DependencyEdge[] {
	return [...edges].sort((left, right) => {
		const resolutionDifference =
			RESOLUTION_RANK[left.resolution]
			- RESOLUTION_RANK[right.resolution];

		if (resolutionDifference !== 0) {
			return resolutionDifference;
		}

		const kindDifference =
			left.kind.localeCompare(right.kind);

		if (kindDifference !== 0) {
			return kindDifference;
		}

		const sourceDifference =
			left.sourcePath.localeCompare(
				right.sourcePath
			);

		if (sourceDifference !== 0) {
			return sourceDifference;
		}

		return (left.targetPath ?? '').localeCompare(
			right.targetPath ?? ''
		);
	});
}

export function normalizeDependencyMap(
	value: unknown
): DependencyMap {
	if (!value || typeof value !== 'object') {
		return createEmptyDependencyMap();
	}

	const raw = value as {
		edges?: unknown;
	};

	return {
		version: 1,
		edges: sortDependencyEdges(
			Array.isArray(raw.edges)
				? raw.edges.map(normalizeEdge)
				: []
		),
	};
}

export function validateDependencyMap(
	dependencyMap: DependencyMap
): DependencyMapValidationIssue[] {
	const issues: DependencyMapValidationIssue[] = [];

	if (dependencyMap.version !== 1) {
		issues.push({
			field: 'version',
			message:
				'Only dependency map version 1 is supported.',
		});
	}

	dependencyMap.edges.forEach((edge, edgeIndex) => {
		if (!edge.sourcePath.trim()) {
			issues.push({
				field: 'sourcePath',
				edgeIndex,
				message:
					'Dependency edge sourcePath cannot be empty.',
			});
		}

		if (!edge.kind.trim()) {
			issues.push({
				field: 'kind',
				edgeIndex,
				message:
					'Dependency edge kind cannot be empty.',
			});
		}

		if (
			(edge.resolution === 'exact'
				|| edge.resolution === 'inferred')
			&& !edge.targetPath?.trim()
		) {
			issues.push({
				field: 'targetPath',
				edgeIndex,
				message:
					`${edge.resolution} dependency edges require a targetPath.`,
			});
		}

		if (edge.evidence.length === 0) {
			issues.push({
				field: 'evidence',
				edgeIndex,
				message:
					'Dependency edges require at least one evidence record.',
			});
		}

		edge.evidence.forEach(
			(evidence, evidenceIndex) => {
				if (!evidence.kind.trim()) {
					issues.push({
						field:
							`evidence[${evidenceIndex}].kind`,
						edgeIndex,
						message:
							'Dependency evidence kind cannot be empty.',
					});
				}

				if (!evidence.detail.trim()) {
					issues.push({
						field:
							`evidence[${evidenceIndex}].detail`,
						edgeIndex,
						message:
							'Dependency evidence detail cannot be empty.',
					});
				}
			}
		);
	});

	return issues;
}

export function getDependencyMapPath(
	config: AiDevConfig
): string {
	return path.posix.join(
		getConfiguredDocsDir(config).replace(/\/+$/, ''),
		DEPENDENCY_MAP_FILE_NAME
	);
}

export async function readDependencyMap(
	workspaceRoot: string,
	config: AiDevConfig
): Promise<DependencyMap> {
	const dependencyMapPath = path.resolve(
		workspaceRoot,
		getDependencyMapPath(config)
	);

	try {
		const raw = await fs.readFile(
			dependencyMapPath,
			'utf8'
		);
		const parsed = JSON.parse(raw) as unknown;
		const dependencyMap =
			normalizeDependencyMap(parsed);
		const issues =
			validateDependencyMap(dependencyMap);

		if (issues.length > 0) {
			throw new Error(
				issues
					.map((issue) => issue.message)
					.join(' ')
			);
		}

		return dependencyMap;
	} catch (error) {
		if (
			(error as NodeJS.ErrnoException).code
			=== 'ENOENT'
		) {
			return createEmptyDependencyMap();
		}

		if (error instanceof SyntaxError) {
			throw new Error(
				`Invalid JSON in ${DEPENDENCY_MAP_FILE_NAME}: ${error.message}`
			);
		}

		throw error;
	}
}

export async function writeDependencyMap(
	workspaceRoot: string,
	config: AiDevConfig,
	dependencyMap: DependencyMap
): Promise<void> {
	const normalized =
		normalizeDependencyMap(dependencyMap);
	const issues =
		validateDependencyMap(normalized);

	if (issues.length > 0) {
		throw new Error(
			issues
				.map((issue) => issue.message)
				.join(' ')
		);
	}

	const dependencyMapPath = path.resolve(
		workspaceRoot,
		getDependencyMapPath(config)
	);
	const temporaryPath =
		`${dependencyMapPath}.tmp-${Date.now()}`;

	await fs.mkdir(
		path.dirname(dependencyMapPath),
		{ recursive: true }
	);

	try {
		await fs.writeFile(
			temporaryPath,
			`${JSON.stringify(normalized, null, 2)}\n`,
			'utf8'
		);
		await fs.rename(
			temporaryPath,
			dependencyMapPath
		);
	} catch (error) {
		try {
			await fs.unlink(temporaryPath);
		} catch {
			// Preserve the original persistence failure.
		}

		throw error;
	}
}

export function findOutgoingDependencyEdges(
	dependencyMap: DependencyMap,
	sourcePath: string,
	options?: {
		kinds?: string[];
		includeInferred?: boolean;
		includeUnresolved?: boolean;
	}
): DependencyEdge[] {
	const normalizedSourcePath =
		normalizePathForMarkdown(sourcePath);
	const allowedKinds = options?.kinds
		? new Set(options.kinds)
		: undefined;
	const includeInferred =
		options?.includeInferred ?? true;
	const includeUnresolved =
		options?.includeUnresolved ?? false;

	return sortDependencyEdges(
		dependencyMap.edges.filter((edge) => {
			if (
				normalizePathForMarkdown(edge.sourcePath)
				!== normalizedSourcePath
			) {
				return false;
			}

			if (
				allowedKinds
				&& !allowedKinds.has(edge.kind)
			) {
				return false;
			}

			if (
				edge.resolution === 'inferred'
				&& !includeInferred
			) {
				return false;
			}

			if (
				(edge.resolution === 'ambiguous'
					|| edge.resolution === 'unresolved')
				&& !includeUnresolved
			) {
				return false;
			}

			return true;
		})
	);
}
