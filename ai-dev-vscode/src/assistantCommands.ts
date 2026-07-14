export interface AssistantCommandOption {
	long: string;
	short: string;
	description: string;
	group?: string;
}

export interface AssistantCommandDefinition {
	name: string;
	description: string;
	details: string;
	usage: string;
	options: AssistantCommandOption[];
}

const HELP_OPTION: AssistantCommandOption = {
	long: '--help',
	short: '-h',
	description: 'Show command help',
};

export const ASSISTANT_COMMAND_DEFINITIONS: AssistantCommandDefinition[] = [
	{
		name: '/ask',
		description: 'Ask the assistant a question',
		details:
			'Ask AI Dev a question and optionally control where it looks for the answer.',
		usage: '/ask [route] <question>',
		options: [
			{
				long: '--auto',
				short: '-a',
				description: 'Choose the best route',
				group: 'route',
			},
			{
				long: '--summary',
				short: '-s',
				description: 'Use summary documentation only',
				group: 'route',
			},
			{
				long: '--knowledgebase',
				short: '-k',
				description: 'Use the knowledge base only',
				group: 'route',
			},
			{
				long: '--chat',
				short: '-c',
				description: 'Bypass project routing',
				group: 'route',
			},
			HELP_OPTION,
		],
	},
	{
		name: '/summarize',
		description: 'Generate or update documentation summaries',
		details:
			'Generate summary documentation for a file, directory, or glob expression.',
		usage: '/summarize <path-or-glob> [options]',
		options: [
			{
				long: '--smoketest',
				short: '-s',
				description: 'Preview matched work without model calls or writes',
			},
			{
				long: '--config',
				short: '-c',
				description: 'Open summarization configuration',
			},
			HELP_OPTION,
		],
	},
	{
		name: '/review',
		description: 'Review project artifacts',
		details:
			'Review project documentation or another supported target for problems and inconsistencies.',
		usage: '/review [target] [options]',
		options: [
			{
				long: '--docs',
				short: '-d',
				description: 'Review documentation',
			},
			{
				long: '--code',
				short: '-c',
				description: 'Review source code',
			},
			{
				long: '--tests',
				short: '-t',
				description: 'Review tests',
			},
			{
				long: '--ticket',
				short: '-i',
				description: 'Review a ticket',
			},
			HELP_OPTION,
		],
	},
	{
		name: '/settings',
		description: 'Configure AI Dev',
		details:
			'Open AI Dev configuration or display help for available settings behavior.',
		usage: '/settings [options]',
		options: [
			{
				long: '--config',
				short: '-c',
				description: 'Open AI Dev configuration',
			},
			HELP_OPTION,
		],
	},
	{
		name: '/showreport',
		description: 'Open the latest detailed report',
		details:
			'Open the detailed report produced by the latest report-generating operation.',
		usage: '/showreport',
		options: [],
	},
	{
		name: '/help',
		description: 'Show available commands',
		details:
			'List available AI Dev commands and basic terminal usage.',
		usage: '/help',
		options: [],
	},
	{
		name: '/exit',
		description: 'Close the assistant',
		details: 'Close the current AI Dev terminal session.',
		usage: '/exit',
		options: [],
	},
];

export function getAssistantCommandDefinition(
	commandName: string
): AssistantCommandDefinition | undefined {
	const normalized = commandName.startsWith('/')
		? commandName.toLowerCase()
		: `/${commandName.toLowerCase()}`;

	return ASSISTANT_COMMAND_DEFINITIONS.find(
		(command) => command.name === normalized
	);
}

export function getAssistantCommandNames(): string[] {
	return ASSISTANT_COMMAND_DEFINITIONS.map(
		(command) => command.name
	);
}

export function formatAssistantCommandSummary(
	command: AssistantCommandDefinition
): string {
	return `${command.name} - ${command.description}`;
}

export function formatAssistantCommandOptionSummary(
	option: AssistantCommandOption
): string {
	return `${option.long}, ${option.short} - ${option.description}`;
}

export function formatAssistantCommandHelp(
	command: AssistantCommandDefinition
): string[] {
	const lines = [
		`${command.name} - ${command.description}`,
		'',
		command.details,
		'',
		'Usage:',
		`  ${command.usage}`,
	];

	if (command.options.length > 0) {
		lines.push('', 'Options:');

		for (const option of command.options) {
			lines.push(
				`  ${formatAssistantCommandOptionSummary(option)}`
			);
		}
	}

	return lines;
}

export interface AssistantLookupItem {
	value: string;
	display: string;
	kind: 'command' | 'option';
	matchKind: 'prefix' | 'substring';
}

function optionMatchesQuery(
	option: AssistantCommandOption,
	query: string
): boolean {
	if (!query || query === '-') {
		return true;
	}

	const normalized = query.toLowerCase();
	return (
		option.long.toLowerCase().startsWith(normalized)
		|| option.short.toLowerCase().startsWith(normalized)
	);
}

export function getAssistantLookupItems(
	input: string
): AssistantLookupItem[] {
	const normalizedInput = input.replace(/^\//, '');
	const hasWhitespace = /\s/.test(normalizedInput);

	if (!hasWhitespace) {
		const query = normalizedInput.trim().toLowerCase();

		const matches: AssistantLookupItem[] = [];

		for (const command of ASSISTANT_COMMAND_DEFINITIONS) {
			const commandName =
				command.name.slice(1).toLowerCase();

			let matchKind: AssistantLookupItem['matchKind']
				| undefined;

			if (!query || commandName.startsWith(query)) {
				matchKind = 'prefix';
			} else if (commandName.includes(query)) {
				matchKind = 'substring';
			}

			if (!matchKind) {
				continue;
			}

			matches.push({
				value: command.name.slice(1),
				display:
					formatAssistantCommandSummary(command),
				kind: 'command',
				matchKind,
			});
		}

		matches.sort((left, right) => {
			if (left.matchKind !== right.matchKind) {
				return left.matchKind === 'prefix' ? -1 : 1;
			}

			return left.value.localeCompare(right.value);
		});

		return matches;
	}

	const endsWithWhitespace = /\s$/.test(normalizedInput);
	const tokens = normalizedInput.trim().split(/\s+/);
	const commandToken = tokens.shift() ?? '';
	const command = getAssistantCommandDefinition(commandToken);

	if (!command || command.options.length === 0) {
		return [];
	}

	const activeToken = endsWithWhitespace
		? ''
		: tokens.pop() ?? '';

	if (activeToken && !activeToken.startsWith('-')) {
		return [];
	}

	const priorTokens = tokens;
	const usedOptions = new Set(
		priorTokens.filter((token) => token.startsWith('-'))
	);

	const usedGroups = new Set<string>();
	for (const option of command.options) {
		if (
			usedOptions.has(option.long)
			|| usedOptions.has(option.short)
		) {
			if (option.group) {
				usedGroups.add(option.group);
			}
		}
	}

	return command.options
		.filter((option) => {
			if (
				usedOptions.has(option.long)
				|| usedOptions.has(option.short)
			) {
				return false;
			}

			if (option.group && usedGroups.has(option.group)) {
				return false;
			}

			return optionMatchesQuery(option, activeToken);
		})
		.map((option) => {
			const completedTokens = [
				commandToken,
				...priorTokens,
				option.long,
			];

			return {
				value: completedTokens.join(' '),
				display: formatAssistantCommandOptionSummary(option),
				kind: 'option' as const,
				matchKind: 'prefix' as const,
			};
		});
}
