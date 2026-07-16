import * as fs from 'node:fs/promises';
import * as path from 'node:path';

export const SUMMARIZATION_CONFIG_FILE_NAME =
	'.ai-dev-summarization.json';

export const DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS = [
	'Summarize behavior and purpose rather than merely describing syntax.',
	'Identify important inputs, outputs, side effects, configuration, and dependencies.',
	'Explain how the file participates in the larger system when that can be determined.',
	'For orchestration files, capture the ordered execution lifecycle, gating conditions, writes and downstream refreshes, cancellation behavior, and which completed work remains valid when later steps fail.',
	'Ignore repetitive boilerplate unless it materially affects behavior.',
	'Treat source and executable configuration as final authority.',
	'Flag unresolved or missing dependencies instead of silently guessing.',
	'Update the existing scoped summary rather than creating a standalone per-file document.',
].join('\n');

export interface SummarizationDependencyStrategy {
	follow: string[];
	maxDepth: number;
	maxFiles: number;
	maxChars: number;
	includeInferred: boolean;
}

export interface SummarizationRule {
	id: string;
	name: string;
	glob: string;
	priority: number;
	enabled: boolean;
	instructions: string;
	dependencyStrategy?: SummarizationDependencyStrategy;
}

export interface SummarizationConfig {
	version: 1;
	generalInstructions: string;
	rules: SummarizationRule[];
}

export interface SummarizationConfigValidationIssue {
	field: string;
	message: string;
	ruleId?: string;
}

export interface ResolvedSummarizationInstructions {
	generalInstructions: string;
	matchingRules: SummarizationRule[];
	combinedInstructions: string;
	dependencyStrategy?: SummarizationDependencyStrategy;
}

export function createDefaultSummarizationConfig():
SummarizationConfig {
	return {
		version: 1,
		generalInstructions:
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
		rules: [],
	};
}

function normalizePathForMatching(filePath: string): string {
	return filePath
		.replace(/\\/g, '/')
		.replace(/^\.\//, '');
}

function escapeRegExp(value: string): string {
	return value.replace(/[|\\{}()[\]^$+?.]/g, '\\$&');
}

export function validateSummarizationGlobSyntax(
	glob: string
): string | undefined {
	const trimmed = glob.trim();

	if (!trimmed) {
		return 'A glob pattern is required.';
	}

	const pairs: Array<[string, string]> = [
		['[', ']'],
		['{', '}'],
	];

	for (const [open, close] of pairs) {
		let depth = 0;

		for (const character of trimmed) {
			if (character === open) {
				depth += 1;
			} else if (character === close) {
				depth -= 1;

				if (depth < 0) {
					return `Unmatched "${close}" in glob pattern.`;
				}
			}
		}

		if (depth > 0) {
			return `Unmatched "${open}" in glob pattern.`;
		}
	}

	return undefined;
}

export function globToSummarizationRegExp(
	glob: string
): RegExp {
	const normalized = normalizePathForMatching(glob.trim());
	let pattern = '';

	for (let index = 0; index < normalized.length; index += 1) {
		const character = normalized[index];

		if (character === '*') {
			const nextCharacter = normalized[index + 1];

			if (nextCharacter === '*') {
				const followingCharacter = normalized[index + 2];

				if (followingCharacter === '/') {
					pattern += '(?:.*/)?';
					index += 2;
				} else {
					pattern += '.*';
					index += 1;
				}
			} else {
				pattern += '[^/]*';
			}

			continue;
		}

		if (character === '?') {
			pattern += '[^/]';
			continue;
		}

		pattern += escapeRegExp(character);
	}

	return new RegExp(`^${pattern}$`);
}

export function matchesSummarizationGlob(
	filePath: string,
	glob: string
): boolean {
	return globToSummarizationRegExp(glob).test(
		normalizePathForMatching(filePath)
	);
}

export function validateSummarizationConfig(
	config: SummarizationConfig
): SummarizationConfigValidationIssue[] {
	const issues: SummarizationConfigValidationIssue[] = [];

	if (config.version !== 1) {
		issues.push({
			field: 'version',
			message: 'Only summarization configuration version 1 is supported.',
		});
	}

	if (!config.generalInstructions.trim()) {
		issues.push({
			field: 'generalInstructions',
			message: 'General summarization instructions cannot be empty.',
		});
	}

	const seenIds = new Set<string>();

	for (const rule of config.rules) {
		if (!rule.id.trim()) {
			issues.push({
				field: 'id',
				ruleId: rule.id,
				message: 'Rule ID cannot be empty.',
			});
		} else if (seenIds.has(rule.id)) {
			issues.push({
				field: 'id',
				ruleId: rule.id,
				message: `Duplicate rule ID: ${rule.id}`,
			});
		}

		seenIds.add(rule.id);

		if (!rule.name.trim()) {
			issues.push({
				field: 'name',
				ruleId: rule.id,
				message: 'Rule name cannot be empty.',
			});
		}

		const globIssue =
			validateSummarizationGlobSyntax(rule.glob);

		if (globIssue) {
			issues.push({
				field: 'glob',
				ruleId: rule.id,
				message: globIssue,
			});
		}

		if (!Number.isFinite(rule.priority)) {
			issues.push({
				field: 'priority',
				ruleId: rule.id,
				message: 'Rule priority must be a finite number.',
			});
		}

		if (!rule.instructions.trim()) {
			issues.push({
				field: 'instructions',
				ruleId: rule.id,
				message: 'Rule instructions cannot be empty.',
			});
		}

		const strategy = rule.dependencyStrategy;

		if (strategy) {
			if (strategy.follow.length === 0) {
				issues.push({
					field: 'dependencyStrategy.follow',
					ruleId: rule.id,
					message:
						'Dependency strategy follow must contain at least one relationship kind.',
				});
			}

			if (
				strategy.follow.some(
					(kind) => !kind.trim()
				)
			) {
				issues.push({
					field: 'dependencyStrategy.follow',
					ruleId: rule.id,
					message:
						'Dependency relationship kinds cannot be empty.',
				});
			}

			if (
				!Number.isInteger(strategy.maxDepth)
				|| strategy.maxDepth < 1
				|| strategy.maxDepth > 10
			) {
				issues.push({
					field: 'dependencyStrategy.maxDepth',
					ruleId: rule.id,
					message:
						'Dependency strategy maxDepth must be an integer from 1 through 10.',
				});
			}

			if (
				!Number.isInteger(strategy.maxFiles)
				|| strategy.maxFiles < 1
			) {
				issues.push({
					field: 'dependencyStrategy.maxFiles',
					ruleId: rule.id,
					message:
						'Dependency strategy maxFiles must be a positive integer.',
				});
			}

			if (
				!Number.isInteger(strategy.maxChars)
				|| strategy.maxChars < 1
			) {
				issues.push({
					field: 'dependencyStrategy.maxChars',
					ruleId: rule.id,
					message:
						'Dependency strategy maxChars must be a positive integer.',
				});
			}
		}
	}

	return issues;
}

function normalizeDependencyStrategy(
	value: unknown
): SummarizationDependencyStrategy | undefined {
	if (!value || typeof value !== 'object') {
		return undefined;
	}

	const raw = value as Partial<
		SummarizationDependencyStrategy
	>;

	const follow = Array.isArray(raw.follow)
		? [
			...new Set(
				raw.follow
					.filter(
						(kind): kind is string =>
							typeof kind === 'string'
							&& kind.trim().length > 0
					)
					.map((kind) => kind.trim())
			),
		]
		: [];

	return {
		follow,
		maxDepth:
			typeof raw.maxDepth === 'number'
				? Math.floor(raw.maxDepth)
				: 1,
		maxFiles:
			typeof raw.maxFiles === 'number'
				? Math.floor(raw.maxFiles)
				: 4,
		maxChars:
			typeof raw.maxChars === 'number'
				? Math.floor(raw.maxChars)
				: 24000,
		includeInferred:
			typeof raw.includeInferred === 'boolean'
				? raw.includeInferred
				: false,
	};
}

function normalizeRule(
	value: Partial<SummarizationRule>,
	index: number
): SummarizationRule {
	const dependencyStrategy =
		normalizeDependencyStrategy(
			value.dependencyStrategy
		);

	return {
		id:
			typeof value.id === 'string' && value.id.trim()
				? value.id.trim()
				: `rule-${index + 1}`,
		name:
			typeof value.name === 'string'
				? value.name.trim()
				: '',
		glob:
			typeof value.glob === 'string'
				? value.glob.trim()
				: '',
		priority:
			typeof value.priority === 'number'
				? value.priority
				: Number(value.priority ?? 0),
		enabled:
			typeof value.enabled === 'boolean'
				? value.enabled
				: true,
		instructions:
			typeof value.instructions === 'string'
				? value.instructions.trim()
				: '',
		...(dependencyStrategy
			? { dependencyStrategy }
			: {}),
	};
}

export function normalizeSummarizationConfig(
	value: unknown
): SummarizationConfig {
	if (!value || typeof value !== 'object') {
		return createDefaultSummarizationConfig();
	}

	const raw = value as {
		version?: unknown;
		generalInstructions?: unknown;
		rules?: unknown;
	};

	const rules = Array.isArray(raw.rules)
		? raw.rules.map((rule, index) =>
			normalizeRule(
				rule && typeof rule === 'object'
					? rule as Partial<SummarizationRule>
					: {},
				index
			)
		)
		: [];

	return {
		version: 1,
		generalInstructions:
			typeof raw.generalInstructions === 'string'
			&& raw.generalInstructions.trim()
				? raw.generalInstructions.trim()
				: DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
		rules,
	};
}

export async function readSummarizationConfig(
	workspaceRoot: string
): Promise<SummarizationConfig> {
	const configPath = path.join(
		workspaceRoot,
		SUMMARIZATION_CONFIG_FILE_NAME
	);

	try {
		const raw = await fs.readFile(configPath, 'utf8');
		const parsed = JSON.parse(raw) as unknown;
		const config = normalizeSummarizationConfig(parsed);
		const issues = validateSummarizationConfig(config);

		if (issues.length > 0) {
			throw new Error(
				issues
					.map((issue) => issue.message)
					.join(' ')
			);
		}

		return config;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
			return createDefaultSummarizationConfig();
		}

		if (error instanceof SyntaxError) {
			throw new Error(
				`Invalid JSON in ${SUMMARIZATION_CONFIG_FILE_NAME}: ${error.message}`
			);
		}

		throw error;
	}
}

export async function writeSummarizationConfig(
	workspaceRoot: string,
	config: SummarizationConfig
): Promise<void> {
	const normalized = normalizeSummarizationConfig(config);
	const issues = validateSummarizationConfig(normalized);

	if (issues.length > 0) {
		throw new Error(
			issues
				.map((issue) => issue.message)
				.join(' ')
		);
	}

	const configPath = path.join(
		workspaceRoot,
		SUMMARIZATION_CONFIG_FILE_NAME
	);
	const temporaryPath =
		`${configPath}.tmp-${Date.now()}`;

	try {
		await fs.writeFile(
			temporaryPath,
			`${JSON.stringify(normalized, null, 2)}\n`,
			'utf8'
		);
		await fs.rename(temporaryPath, configPath);
	} catch (error) {
		try {
			await fs.unlink(temporaryPath);
		} catch {
			// Preserve the original write failure.
		}

		throw error;
	}
}

export function resolveSummarizationInstructions(
	config: SummarizationConfig,
	sourcePath: string
): ResolvedSummarizationInstructions {
	const matchingRules = config.rules
		.filter(
			(rule) =>
				rule.enabled
				&& matchesSummarizationGlob(
					sourcePath,
					rule.glob
				)
		)
		.sort((left, right) => {
			if (left.priority !== right.priority) {
				return left.priority - right.priority;
			}

			return left.name.localeCompare(right.name);
		});

	const specializedSections = matchingRules.map(
		(rule) => [
			`Rule: ${rule.name}`,
			`Pattern: ${rule.glob}`,
			rule.instructions,
		].join('\n')
	);

	const dependencyStrategy = [
		...matchingRules,
	]
		.reverse()
		.find(
			(rule) =>
				rule.dependencyStrategy !== undefined
		)
		?.dependencyStrategy;

	return {
		generalInstructions: config.generalInstructions,
		matchingRules,
		dependencyStrategy,
		combinedInstructions: [
			'General summarization instructions:',
			config.generalInstructions,
			...specializedSections.flatMap(
				(section) => [
					'',
					'Specialized summarization instructions:',
					section,
				]
			),
		].join('\n'),
	};
}

export function injectSummarizationInstructions(
	prompt: string,
	resolved: ResolvedSummarizationInstructions
): string {
	const guidanceBlock = [
		'Configured summarization guidance:',
		'',
		resolved.combinedInstructions,
		'',
		'Guidance precedence:',
		'Apply the general instructions first.',
		'Then apply every matching specialized rule in the listed order.',
		'When guidance overlaps, the later specialized instruction is more specific.',
	].join('\n');

	const instructionMarker = '\nInstructions:\n';
	const markerIndex = prompt.lastIndexOf(instructionMarker);

	if (markerIndex < 0) {
		return [
			prompt.trimEnd(),
			'',
			guidanceBlock,
		].join('\n');
	}

	return [
		prompt.slice(0, markerIndex).trimEnd(),
		'',
		guidanceBlock,
		prompt.slice(markerIndex),
	].join('\n');
}
