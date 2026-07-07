import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { normalizePathForMarkdown } from './workspace';

const execFileAsync = promisify(execFile);

export interface ChangedFileWithContent {
	relativePath: string;
	contents: string;
}

export interface GitRenameRecord {
	oldPath: string;
	newPath: string;
	score?: number;
}

export interface GitFileDiff {
	relativePath: string;
	diff: string;
}

function parseGitDiffOutput(stdout: string): string[] {
	return stdout
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line.length > 0);
}

async function readGitFileList(workspaceRoot: string, args: string[]): Promise<string[]> {
	try {
		const result = await execFileAsync('git', args, { cwd: workspaceRoot });
		return parseGitDiffOutput(result.stdout);
	} catch {
		return [];
	}
}

export async function getGitChangedFiles(workspaceRoot: string): Promise<string[]> {
	const preferredFiles = await readGitFileList(workspaceRoot, ['diff', '--name-only', 'origin/main...HEAD']);
	const unstagedFiles = await readGitFileList(workspaceRoot, ['diff', '--name-only']);
	const stagedFiles = await readGitFileList(workspaceRoot, ['diff', '--cached', '--name-only']);
	const untrackedFiles = await readGitFileList(workspaceRoot, ['ls-files', '--others', '--exclude-standard']);

	const orderedFiles = [...preferredFiles, ...unstagedFiles, ...stagedFiles, ...untrackedFiles];
	return Array.from(new Set(orderedFiles));
}

function parseGitRenameStatusOutput(stdout: string): GitRenameRecord[] {
	const lines = stdout
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line.length > 0);

	const records: GitRenameRecord[] = [];
	for (const line of lines) {
		const parts = line.split('\t');
		if (parts.length < 3) {
			continue;
		}

		const statusToken = parts[0] ?? '';
		if (!statusToken.startsWith('R')) {
			continue;
		}

		const oldPath = parts[1]?.trim();
		const newPath = parts[2]?.trim();
		if (!oldPath || !newPath) {
			continue;
		}

		const scoreText = statusToken.slice(1);
		const parsedScore = Number.parseInt(scoreText, 10);
		records.push({
			oldPath,
			newPath,
			score: Number.isFinite(parsedScore) ? parsedScore : undefined,
		});
	}

	return records;
}

async function readGitRenameRecords(workspaceRoot: string, args: string[]): Promise<GitRenameRecord[]> {
	try {
		const result = await execFileAsync('git', args, { cwd: workspaceRoot });
		return parseGitRenameStatusOutput(result.stdout);
	} catch {
		return [];
	}
}

export async function getGitRenameRecords(workspaceRoot: string): Promise<GitRenameRecord[]> {
	const headRenames = await readGitRenameRecords(workspaceRoot, ['diff', '--name-status', '--find-renames', 'HEAD']);
	if (headRenames.length > 0) {
		return headRenames;
	}

	const upstreamRenames = await readGitRenameRecords(workspaceRoot, ['diff', '--name-status', '--find-renames', 'origin/main...HEAD']);
	if (upstreamRenames.length > 0) {
		return upstreamRenames;
	}

	const stagedRenames = await readGitRenameRecords(workspaceRoot, ['diff', '--cached', '--name-status', '--find-renames']);
	if (stagedRenames.length > 0) {
		return stagedRenames;
	}

	const unstagedRenames = await readGitRenameRecords(workspaceRoot, ['diff', '--name-status', '--find-renames']);
	return unstagedRenames;
}

async function readGitDiffForPath(workspaceRoot: string, relativePath: string, argsPrefix: string[]): Promise<string> {
	try {
		const result = await execFileAsync('git', [...argsPrefix, '--', relativePath], { cwd: workspaceRoot });
		return result.stdout;
	} catch {
		return '';
	}
}

export async function getGitDiffForFiles(workspaceRoot: string, changedFiles: string[]): Promise<GitFileDiff[]> {
	const diffs = await Promise.all(
		changedFiles.map(async (relativePath) => {
			const normalizedPath = normalizePathForMarkdown(relativePath);
			const unstagedDiff = await readGitDiffForPath(workspaceRoot, normalizedPath, ['diff']);
			if (unstagedDiff.trim().length > 0) {
				return { relativePath: normalizedPath, diff: unstagedDiff };
			}

			const stagedDiff = await readGitDiffForPath(workspaceRoot, normalizedPath, ['diff', '--cached']);
			if (stagedDiff.trim().length > 0) {
				return { relativePath: normalizedPath, diff: stagedDiff };
			}

			const headDiff = await readGitDiffForPath(workspaceRoot, normalizedPath, ['diff', 'HEAD']);
			if (headDiff.trim().length > 0) {
				return { relativePath: normalizedPath, diff: headDiff };
			}

			return {
				relativePath: normalizedPath,
				diff: '',
			};
		})
	);

	return diffs;
}

export async function existingChangedFilesWithContent(
	workspaceRoot: string,
	changedFiles: string[]
): Promise<ChangedFileWithContent[]> {
	const files = await Promise.all(
		changedFiles.map(async (relativePath) => {
			const absolutePath = path.join(workspaceRoot, relativePath);
			try {
				const stat = await fs.stat(absolutePath);
				if (!stat.isFile()) {
					return undefined;
				}
				const contents = await fs.readFile(absolutePath, 'utf8');
				return {
					relativePath: normalizePathForMarkdown(relativePath),
					contents,
				};
			} catch {
				return undefined;
			}
		})
	);

	return files.filter((file): file is ChangedFileWithContent => file !== undefined);
}