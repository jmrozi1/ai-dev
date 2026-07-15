import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {
	getYamlNestedValue,
	type AiProviderMode,
} from './config';
import {
	FALLBACK_BATCH_EXCLUDE_GLOBS,
	parseYamlList,
} from './sourceDiscovery';
import { openSettingsWebview } from './settingsView';
import { getOpenWorkspaceRoot } from './workspace';

function getIndentation(line: string): string {
	const match = line.match(/^[ \t]*/);
	return match ? match[0] : '';
}

function withOriginalLineEnding(original: string, updated: string): string {
	const lineEnding = original.includes('\r\n') ? '\r\n' : '\n';
	const normalizedUpdated = updated.replace(/\n/g, lineEnding);
	if (original.endsWith('\n') || original.endsWith('\r\n')) {
		return normalizedUpdated.endsWith(lineEnding) ? normalizedUpdated : `${normalizedUpdated}${lineEnding}`;
	}

	return normalizedUpdated.endsWith(lineEnding)
		? normalizedUpdated.slice(0, -lineEnding.length)
		: normalizedUpdated;
}

function updateAiProviderModeInYaml(yamlContent: string, selectedMode: AiProviderMode): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const modeValue = `"${selectedMode}"`;
	const aiProviderLinePattern = /^\s*aiProvider\s*:/;
	const modeLinePattern = /^\s*mode\s*:/;

	const aiProviderIndex = lines.findIndex((line) => aiProviderLinePattern.test(line));

	if (aiProviderIndex >= 0) {
		const aiProviderIndent = getIndentation(lines[aiProviderIndex]).length;
		let blockEnd = lines.length;
		for (let index = aiProviderIndex + 1; index < lines.length; index += 1) {
			const line = lines[index];
			const trimmed = line.trim();
			if (trimmed.length === 0 || trimmed.startsWith('#')) {
				continue;
			}

			const indent = getIndentation(line).length;
			if (indent <= aiProviderIndent) {
				blockEnd = index;
				break;
			}
		}

		for (let index = aiProviderIndex + 1; index < blockEnd; index += 1) {
			if (!modeLinePattern.test(lines[index])) {
				continue;
			}

			const modeIndentation = getIndentation(lines[index]);
			lines[index] = `${modeIndentation}mode: ${modeValue}`;
			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const childIndentation = (() => {
			for (let index = aiProviderIndex + 1; index < blockEnd; index += 1) {
				const line = lines[index];
				const trimmed = line.trim();
				if (trimmed.length === 0 || trimmed.startsWith('#')) {
					continue;
				}

				const indentation = getIndentation(line);
				if (indentation.length > aiProviderIndent) {
					return indentation;
				}
			}

			return `${getIndentation(lines[aiProviderIndex])}  `;
		})();

		lines.splice(aiProviderIndex + 1, 0, `${childIndentation}mode: ${modeValue}`);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const base = normalized.trimEnd();
	const suffix = base.length > 0 ? '\n\naiProvider:\n  mode: ' : 'aiProvider:\n  mode: ';
	const updated = `${base}${suffix}${modeValue}`;
	return withOriginalLineEnding(yamlContent, updated);
}

function quoteYamlString(value: string): string {
	const escaped = value
		.replace(/\\/g, '\\\\')
		.replace(/"/g, '\\"');
	return `"${escaped}"`;
}

function getTopLevelSectionBounds(lines: string[], section: string): {
	sectionIndex: number;
	sectionIndent: number;
	blockEnd: number;
} | undefined {
	const sectionLinePattern = new RegExp(`^\\s*${section}\\s*:\\s*$`);
	const sectionIndex = lines.findIndex((line) => sectionLinePattern.test(line));
	if (sectionIndex < 0) {
		return undefined;
	}

	const sectionIndent = getIndentation(lines[sectionIndex]).length;
	let blockEnd = lines.length;
	for (let index = sectionIndex + 1; index < lines.length; index += 1) {
		const line = lines[index];
		const trimmed = line.trim();
		if (trimmed.length === 0 || trimmed.startsWith('#')) {
			continue;
		}

		const indent = getIndentation(line).length;
		if (indent <= sectionIndent) {
			blockEnd = index;
			break;
		}
	}

	return { sectionIndex, sectionIndent, blockEnd };
}

function updateYamlSectionScalarValue(yamlContent: string, section: string, key: string, value: string): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const quotedValue = quoteYamlString(value);
	const bounds = getTopLevelSectionBounds(lines, section);

	if (bounds) {
		const { sectionIndex, sectionIndent, blockEnd } = bounds;
		const keyPattern = new RegExp(`^\\s{${sectionIndent + 2}}${key}\\s*:`);

		for (let index = sectionIndex + 1; index < blockEnd; index += 1) {
			if (!keyPattern.test(lines[index])) {
				continue;
			}

			const keyIndentation = getIndentation(lines[index]);
			const keyIndentLength = keyIndentation.length;
			lines[index] = `${keyIndentation}${key}: ${quotedValue}`;

			let removalEnd = index + 1;
			while (removalEnd < blockEnd) {
				const childLine = lines[removalEnd];
				const childTrimmed = childLine.trim();
				if (childTrimmed.length === 0) {
					removalEnd += 1;
					continue;
				}

				if (childTrimmed.startsWith('#')) {
					removalEnd += 1;
					continue;
				}

				const childIndent = getIndentation(childLine).length;
				if (childIndent <= keyIndentLength) {
					break;
				}

				removalEnd += 1;
			}

			if (removalEnd > index + 1) {
				lines.splice(index + 1, removalEnd - (index + 1));
			}

			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const childIndentation = `${getIndentation(lines[sectionIndex])}  `;
		lines.splice(sectionIndex + 1, 0, `${childIndentation}${key}: ${quotedValue}`);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const base = normalized.trimEnd();
	const suffix = base.length > 0
		? `\n\n${section}:\n  ${key}: ${quotedValue}`
		: `${section}:\n  ${key}: ${quotedValue}`;
	return withOriginalLineEnding(yamlContent, `${base}${suffix}`);
}

function updateYamlSectionListValue(yamlContent: string, section: string, key: string, values: string[]): string {
	const normalized = yamlContent.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	const bounds = getTopLevelSectionBounds(lines, section);

	if (bounds) {
		const { sectionIndex, sectionIndent, blockEnd } = bounds;
		const keyPattern = new RegExp(`^\\s{${sectionIndent + 2}}${key}\\s*:`);
		const keyIndentation = `${getIndentation(lines[sectionIndex])}  `;
		const itemIndentation = `${keyIndentation}  `;

		for (let index = sectionIndex + 1; index < blockEnd; index += 1) {
			if (!keyPattern.test(lines[index])) {
				continue;
			}

			const existingKeyIndentation = getIndentation(lines[index]);
			const keyIndentLength = existingKeyIndentation.length;
			lines[index] = `${existingKeyIndentation}${key}:`;

			let blockRemovalEnd = index + 1;
			while (blockRemovalEnd < blockEnd) {
				const candidate = lines[blockRemovalEnd];
				const trimmed = candidate.trim();
				if (trimmed.length === 0) {
					blockRemovalEnd += 1;
					continue;
				}

				if (trimmed.startsWith('#')) {
					blockRemovalEnd += 1;
					continue;
				}

				const candidateIndent = getIndentation(candidate).length;
				if (candidateIndent <= keyIndentLength) {
					break;
				}

				blockRemovalEnd += 1;
			}

			if (blockRemovalEnd > index + 1) {
				lines.splice(index + 1, blockRemovalEnd - (index + 1));
			}

			const itemLines = values.map((value) => `${itemIndentation}- ${quoteYamlString(value)}`);
			if (itemLines.length > 0) {
				lines.splice(index + 1, 0, ...itemLines);
			}

			return withOriginalLineEnding(yamlContent, lines.join('\n'));
		}

		const insertionLines = [
			`${keyIndentation}${key}:`,
			...values.map((value) => `${itemIndentation}- ${quoteYamlString(value)}`),
		];
		lines.splice(sectionIndex + 1, 0, ...insertionLines);
		return withOriginalLineEnding(yamlContent, lines.join('\n'));
	}

	const listLines = [
		`${section}:`,
		`  ${key}:`,
		...values.map((value) => `    - ${quoteYamlString(value)}`),
	];
	const base = normalized.trimEnd();
	const suffix = base.length > 0 ? `\n\n${listLines.join('\n')}` : listLines.join('\n');
	return withOriginalLineEnding(yamlContent, `${base}${suffix}`);
}

export async function openSettingsCommand(
	context: vscode.ExtensionContext
): Promise<void> {
	const workspaceRoot = getOpenWorkspaceRoot();
	if (!workspaceRoot) {
		await vscode.window.showErrorMessage('No workspace is open.');
		return;
	}

	const aiDevYamlPath = path.join(workspaceRoot, '.ai-dev.yaml');
	let initialPromptOnly = false;
	let initialDocsDir = 'ai-docs';
	let initialBatchInitialSourceGlob = '**/*';
	let initialSourceExcludeGlobs = [...FALLBACK_BATCH_EXCLUDE_GLOBS];
	try {
		const aiDevYamlContents = await fs.readFile(aiDevYamlPath, 'utf8');
		initialPromptOnly = getYamlNestedValue(aiDevYamlContents, 'aiProvider', 'mode')?.trim() === 'prompt-only';
		const configuredDocsDir = getYamlNestedValue(aiDevYamlContents, 'documentation', 'docsDir')?.trim();
		if (configuredDocsDir) {
			initialDocsDir = configuredDocsDir;
		}

		const configuredBatchInitialSourceGlob = getYamlNestedValue(aiDevYamlContents, 'documentation', 'batchInitialSourceGlob')?.trim();
		if (configuredBatchInitialSourceGlob) {
			initialBatchInitialSourceGlob = configuredBatchInitialSourceGlob;
		}

		const configuredSourceExcludeGlobs = parseYamlList(aiDevYamlContents, 'source', 'exclude');
		if (configuredSourceExcludeGlobs.length > 0) {
			initialSourceExcludeGlobs = configuredSourceExcludeGlobs;
		}
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
			const message = error instanceof Error ? error.message : String(error);
			await vscode.window.showErrorMessage(`Failed to read .ai-dev.yaml: ${message}`);
			return;
		}
	}

	openSettingsWebview(context, {
		initialPromptOnly,
		initialDocsDir,
		initialBatchInitialSourceGlob,
		initialSourceExcludeGlobs,
		onSave: async (settings: {
			promptOnly?: boolean;
			docsDir?: string;
			batchInitialSourceGlob?: string;
			sourceExcludeGlobs?: string[];
		}) => {

			let updatedYaml: string;

			try {
				const existingYaml = await fs.readFile(aiDevYamlPath, 'utf8');
				updatedYaml = existingYaml;
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
					updatedYaml = '';
				} else {
					const message = error instanceof Error ? error.message : String(error);
					throw new Error(`Failed to read .ai-dev.yaml: ${message}`);
				}
			}

			if (typeof settings.promptOnly === 'boolean') {
				const selectedMode: AiProviderMode = settings.promptOnly ? 'prompt-only' : 'direct-experimental';
				updatedYaml = updateAiProviderModeInYaml(updatedYaml, selectedMode);
			}

			if (typeof settings.docsDir === 'string') {
				const docsDirValue = settings.docsDir.trim() || 'ai-docs';
				updatedYaml = updateYamlSectionScalarValue(updatedYaml, 'documentation', 'docsDir', docsDirValue);
			}

			if (typeof settings.batchInitialSourceGlob === 'string') {
				const batchInitialSourceGlobValue = settings.batchInitialSourceGlob.trim() || '**/*';
				updatedYaml = updateYamlSectionScalarValue(updatedYaml, 'documentation', 'batchInitialSourceGlob', batchInitialSourceGlobValue);
			}

			if (Array.isArray(settings.sourceExcludeGlobs)) {
				const normalizedGlobs = settings.sourceExcludeGlobs
					.map((glob) => glob.trim())
					.filter((glob) => glob.length > 0);
				updatedYaml = updateYamlSectionListValue(updatedYaml, 'source', 'exclude', normalizedGlobs);
			}

			try {
				await fs.writeFile(aiDevYamlPath, updatedYaml, 'utf8');
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				throw new Error(`Failed to write .ai-dev.yaml: ${message}`);
			}
		},
	});
}
