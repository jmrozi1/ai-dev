import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	isPathInsideDirectory,
} from './sourceDiscovery';
import {
	getOpenWorkspaceRoot,
	normalizePathForMarkdown,
} from './workspace';

const PATH_COMPLETION_EXCLUDED_NAMES = new Set([
	'.git',
	'node_modules',
	'out',
	'dist',
	'build',
	'coverage',
	'.vscode-test',
]);

export async function completeWorkspacePath(
	partialPath: string
): Promise<{ matches: string[] }> {
	const workspaceRoot = getOpenWorkspaceRoot();

	if (!workspaceRoot) {
		return { matches: [] };
	}

	const normalizedPartial =
		normalizePathForMarkdown(partialPath);
	const slashIndex = normalizedPartial.lastIndexOf('/');

	const directoryPart =
		slashIndex >= 0
			? normalizedPartial.slice(0, slashIndex + 1)
			: '';

	const namePrefix =
		slashIndex >= 0
			? normalizedPartial.slice(slashIndex + 1)
			: normalizedPartial;

	const directoryAbsolutePath = path.resolve(
		workspaceRoot,
		directoryPart || '.'
	);

	if (
		!isPathInsideDirectory(
			directoryAbsolutePath,
			workspaceRoot
		)
	) {
		return { matches: [] };
	}

	let entries;

	try {
		entries = await fs.readdir(
			directoryAbsolutePath,
			{ withFileTypes: true }
		);
	} catch {
		return { matches: [] };
	}

	const matches = entries
		.filter(
			(entry) =>
				!PATH_COMPLETION_EXCLUDED_NAMES.has(entry.name)
				&& entry.name.startsWith(namePrefix)
		)
		.map((entry) =>
			`${directoryPart}${entry.name}`
			+ (entry.isDirectory() ? '/' : '')
		)
		.sort((left, right) =>
			left.localeCompare(right)
		);

	return { matches };
}
