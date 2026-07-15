import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {
	normalizePathForMarkdown,
} from './workspace';

let aiDevExtensionRootPath: string | undefined;

export function setAiDevExtensionRootPath(extensionRootPath: string): void {
	aiDevExtensionRootPath = extensionRootPath;
}

function getBundledAiDevCorePath(): string | undefined {
	if (!aiDevExtensionRootPath) {
		return undefined;
	}

	return path.join(aiDevExtensionRootPath, 'vendor', 'ai-dev-core');
}

export interface AiDevConfig {
	raw: string;
	aiDevCorePath?: string;
	aiProviderMode?: string;
	docsDir?: string;
	batchInitialSourceGlob?: string;
}

const DEFAULT_DOCS_DIR = 'ai-docs';
const DEFAULT_AI_PROVIDER_MODE = 'direct-experimental';
const DEFAULT_BATCH_INITIAL_SOURCE_GLOB = '**/*';

export const ARCHITECTURE_SUMMARY_FILE_NAME =
	'architecture-summary.md';

export type AiProviderMode =
	| 'prompt-only'
	| 'direct-experimental';

function isSupportedExecutionMode(
	value: string
): value is AiProviderMode {
	return (
		value === 'prompt-only'
		|| value === 'direct-experimental'
	);
}

export function getExecutionModeFromConfig(
	config: AiDevConfig
):
	| { mode: AiProviderMode }
	| { errorMessage: string } {
	const trimmedMode = config.aiProviderMode?.trim();

	if (!trimmedMode) {
		return { mode: 'direct-experimental' };
	}

	if (!isSupportedExecutionMode(trimmedMode)) {
		return {
			errorMessage:
				'Unsupported aiProvider.mode in .ai-dev.yaml. Supported values: prompt-only, direct-experimental.',
		};
	}

	return { mode: trimmedMode };
}

function getNormalizedDocsDir(
	config: AiDevConfig
): string {
	const configuredDocsDir =
		config.docsDir?.trim() || DEFAULT_DOCS_DIR;

	return normalizePathForMarkdown(
		configuredDocsDir
	).replace(/\/+$/, '');
}

export function getRootSummaryFilePath(
	config: AiDevConfig
): string {
	return path.posix.join(
		getNormalizedDocsDir(config),
		ARCHITECTURE_SUMMARY_FILE_NAME
	);
}

export function getLegacyRootSummaryFilePath(
	config: AiDevConfig
): string {
	return path.posix.join(
		getNormalizedDocsDir(config),
		'summary.md'
	);
}

export function getArchitectureSummaryPath(
	config: AiDevConfig
): string {
	return getRootSummaryFilePath(config);
}

export interface AiDevYamlPromptSection {
	label: string;
	contents: string;
}

export function getAiDevYamlPromptSection(
	config: AiDevConfig
): AiDevYamlPromptSection {
	if (config.raw.trim().length === 0) {
		return {
			label:
				'.ai-dev.yaml: not present; using generic defaults',
			contents:
				'# .ai-dev.yaml not present; using generic defaults',
		};
	}

	return {
		label: '.ai-dev.yaml',
		contents: config.raw,
	};
}

function unquoteYamlValue(value: string): string {
	const trimmed = value.trim();
	if (trimmed.length >= 2) {
		const first = trimmed[0];
		const last = trimmed[trimmed.length - 1];
		if ((first === '"' && last === '"') || (first === '\'' && last === '\'')) {
			return trimmed.slice(1, -1);
		}
	}

	return trimmed;
}

export function getYamlNestedValue(rawYaml: string, section: string, key: string): string | undefined {
	const lines = rawYaml.split(/\r?\n/);
	let inSection = false;

	for (const line of lines) {
		if (!inSection) {
			if (new RegExp(`^${section}:\\s*$`).test(line)) {
				inSection = true;
			}
			continue;
		}

		if (/^\S/.test(line)) {
			break;
		}

		const match = line.match(new RegExp(`^\\s{2}${key}:\\s*(.+)\\s*$`));
		if (match && match[1]) {
			return unquoteYamlValue(match[1]);
		}
	}

	return undefined;
}

function getConfiguredAiDevCorePath(rawYaml: string): string | undefined {
	return getYamlNestedValue(rawYaml, 'aiDevCore', 'path') ?? getBundledAiDevCorePath();
}

export async function readAiDevConfig(workspaceRoot: string): Promise<AiDevConfig> {
	let raw = '';
	try {
		raw = await fs.readFile(path.join(workspaceRoot, '.ai-dev.yaml'), 'utf8');
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
			throw error;
		}
	}

	return {
		raw,
		aiDevCorePath: getConfiguredAiDevCorePath(raw),
		aiProviderMode: getYamlNestedValue(raw, 'aiProvider', 'mode') ?? DEFAULT_AI_PROVIDER_MODE,
		docsDir: getYamlNestedValue(raw, 'documentation', 'docsDir') ?? DEFAULT_DOCS_DIR,
		batchInitialSourceGlob: getYamlNestedValue(raw, 'documentation', 'batchInitialSourceGlob') ?? DEFAULT_BATCH_INITIAL_SOURCE_GLOB,
	};
}

export function resolveAiDevCorePath(workspaceRoot: string, aiDevCorePath: string | undefined): string {
	const configuredPath = aiDevCorePath?.trim();
	if (configuredPath) {
		return path.resolve(workspaceRoot, configuredPath);
	}

	const bundledCorePath = getBundledAiDevCorePath();
	if (bundledCorePath) {
		return bundledCorePath;
	}

	return path.resolve(workspaceRoot, 'ai-dev-core');
}