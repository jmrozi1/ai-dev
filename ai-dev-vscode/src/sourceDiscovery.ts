import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import type { AiDevConfig } from './config';
import {
	NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS,
	matchesAnyGlob,
} from './pathMatching';
import {
	normalizePathForMarkdown,
} from './workspace';

export const FALLBACK_BATCH_EXCLUDE_GLOBS = [
	'.git/**',
	'**/.git/**',
	'.*',
	'**/.*',
	'node_modules/**',
	'**/node_modules/**',
	'dist/**',
	'build/**',
	'out/**',
	'coverage/**',
	'vendor/**',
	'vendors/**',
	'libs/**',
	'Libs/**',
	'**/*.min.*',
	'**/*.generated.*',
	'**/*.lock',
];

export function getConfiguredDocsDir(config: AiDevConfig): string {
	const configuredDocsDir = config.docsDir?.trim();
	if (!configuredDocsDir) {
		return 'ai-docs';
	}

	return normalizePathForMarkdown(configuredDocsDir);
}

export function isPathInsideDirectory(targetPath: string, directoryPath: string): boolean {
	const relativePath = path.relative(directoryPath, targetPath);
	return relativePath === '' || (!relativePath.startsWith('..') && !path.isAbsolute(relativePath));
}

export async function listFilesRecursively(rootDirectory: string): Promise<string[]> {
	const results: string[] = [];
	const pendingDirectories: string[] = [rootDirectory];

	while (pendingDirectories.length > 0) {
		const currentDirectory = pendingDirectories.pop();
		if (!currentDirectory) {
			continue;
		}

		const entries = await fs.readdir(currentDirectory, { withFileTypes: true, encoding: 'utf8' });
		for (const entry of entries) {
			const entryPath = path.join(currentDirectory, entry.name);
			if (entry.isDirectory()) {
				pendingDirectories.push(entryPath);
				continue;
			}

			if (entry.isFile()) {
				results.push(entryPath);
			}
		}
	}

	return results;
}

export function parseYamlList(rawYaml: string, section: string, key: string): string[] {
	const lines = rawYaml.split(/\r?\n/);
	const sectionPattern = new RegExp(`^${section}:\\s*$`);
	const keyPattern = new RegExp(`^\\s{2}${key}:\\s*$`);

	let sectionIndex = -1;
	for (let index = 0; index < lines.length; index += 1) {
		if (sectionPattern.test(lines[index])) {
			sectionIndex = index;
			break;
		}
	}

	if (sectionIndex < 0) {
		return [];
	}

	let keyIndex = -1;
	for (let index = sectionIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		if (/^\S/.test(line)) {
			break;
		}

		if (keyPattern.test(line)) {
			keyIndex = index;
			break;
		}
	}

	if (keyIndex < 0) {
		return [];
	}

	const values: string[] = [];
	for (let index = keyIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		if (/^\S/.test(line)) {
			break;
		}

		if (/^\s{2}[a-zA-Z0-9_-]+:\s*/.test(line)) {
			break;
		}

		const match = line.match(/^\s{4}-\s+(.+)\s*$/);
		if (!match || !match[1]) {
			continue;
		}

		const rawValue = match[1].trim();
		if (rawValue.length === 0) {
			continue;
		}

		if (
			(rawValue.startsWith('"') && rawValue.endsWith('"'))
			|| (rawValue.startsWith('\'') && rawValue.endsWith('\''))
		) {
			values.push(rawValue.slice(1, -1));
			continue;
		}

		values.push(rawValue);
	}

	return values;
}

export function getBatchInitialSourceGlob(config: AiDevConfig): string {
	const configuredGlob = config.batchInitialSourceGlob?.trim();
	return configuredGlob && configuredGlob.length > 0 ? configuredGlob : '**/*';
}

export function normalizeBatchSourceGlob(sourceGlob: string, config: AiDevConfig): string {
	const normalizedSourceGlob = sourceGlob.trim();
	return normalizedSourceGlob.length > 0 ? normalizedSourceGlob : getBatchInitialSourceGlob(config);
}

export function getBatchSourceGlobs(config: AiDevConfig): { excludeGlobs: string[] } {
	const configuredExcludeGlobs =
		parseYamlList(config.raw, 'source', 'exclude');

	const projectExcludeGlobs =
		configuredExcludeGlobs.length > 0
			? configuredExcludeGlobs
			: FALLBACK_BATCH_EXCLUDE_GLOBS;

	return {
		excludeGlobs: [
			...new Set([
				...projectExcludeGlobs,
				...NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS,
			]),
		],
	};
}

export function isConfiguredSourcePath(relativePath: string, excludeGlobs: string[]): boolean {
	return !matchesAnyGlob(relativePath, excludeGlobs);
}

export function isConfiguredSourceCandidatePath(relativePath: string, docsDir: string, excludeGlobs: string[]): boolean {
	const normalizedRelativePath = normalizePathForMarkdown(relativePath);
	const normalizedDocsDir = normalizePathForMarkdown(docsDir).replace(/\/+$/, '');
	if (normalizedRelativePath === normalizedDocsDir || normalizedRelativePath.startsWith(`${normalizedDocsDir}/`)) {
		return false;
	}

	return isConfiguredSourcePath(normalizedRelativePath, excludeGlobs);
}

export async function discoverBatchUnitDocCandidates(workspaceRoot: string, config: AiDevConfig): Promise<string[]> {
	const { excludeGlobs } = getBatchSourceGlobs(config);
	const docsDir = getConfiguredDocsDir(config);
	const docsDirAbsolutePath = path.resolve(workspaceRoot, docsDir);
	const allFiles = await listFilesRecursively(workspaceRoot);
	const candidates = allFiles.filter((absolutePath) => {
		if (isPathInsideDirectory(absolutePath, docsDirAbsolutePath)) {
			return false;
		}

		const relativePath = normalizePathForMarkdown(path.relative(workspaceRoot, absolutePath));
		if (matchesAnyGlob(relativePath, excludeGlobs)) {
			return false;
		}

		return true;
	});

	candidates.sort((left, right) => left.localeCompare(right));
	return candidates;
}
