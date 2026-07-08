import * as fs from 'node:fs/promises';
import * as path from 'node:path';

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
	aiDevCorePathFromYaml?: string;
	aiProviderMode?: string;
	docsDir?: string;
	batchInitialSourceGlob?: string;
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
	const raw = await fs.readFile(path.join(workspaceRoot, '.ai-dev.yaml'), 'utf8');
	return {
		raw,
		aiDevCorePathFromYaml: getConfiguredAiDevCorePath(raw),
		aiProviderMode: getYamlNestedValue(raw, 'aiProvider', 'mode'),
		docsDir: getYamlNestedValue(raw, 'documentation', 'docsDir'),
		batchInitialSourceGlob: getYamlNestedValue(raw, 'documentation', 'batchInitialSourceGlob'),
	};
}

export function resolveAiDevCorePath(workspaceRoot: string, aiDevCorePathFromYaml: string | undefined): string {
	const configuredPath = aiDevCorePathFromYaml?.trim();
	if (configuredPath) {
		return path.resolve(workspaceRoot, configuredPath);
	}

	const bundledCorePath = getBundledAiDevCorePath();
	if (bundledCorePath) {
		return bundledCorePath;
	}

	return path.resolve(workspaceRoot, 'ai-dev-core');
}