export type AssistantMode = 'chat' | 'command';

export interface AssistantInputState {
	mode: AssistantMode;
	input: string;
	history: string[];
	historyIndex: number;
	historyDraft?: {
		mode: AssistantMode;
		input: string;
	};
	tabPressCount: number;
}

export type SubmitKind = 'chat' | 'command';

export interface SubmitResult {
	state: AssistantInputState;
	submittedText?: string;
	submittedKind?: SubmitKind;
}

export interface TabResult {
	state: AssistantInputState;
	listMatches?: string[];
}

export function createAssistantInputState(): AssistantInputState {
	return {
		mode: 'chat',
		input: '',
		history: [],
		historyIndex: -1,
		historyDraft: undefined,
		tabPressCount: 0,
	};
}

function restoreHistoryEntry(entry: string): Pick<AssistantInputState, 'mode' | 'input'> {
	if (entry.startsWith('/')) {
		return {
			mode: 'command',
			input: entry.slice(1),
		};
	}

	return {
		mode: 'chat',
		input: entry,
	};
}

export function getPromptMarker(state: AssistantInputState): '>' | '/' {
	return state.mode === 'command' ? '/' : '>';
}

export function applyTextInput(state: AssistantInputState, text: string): AssistantInputState {
	let next = state;
	for (const char of text) {
		next = applyCharacter(next, char);
	}

	return next;
}

function applyCharacter(state: AssistantInputState, char: string): AssistantInputState {
	if (char === '/' && state.mode === 'chat' && state.input.length === 0) {
		return {
			...state,
			mode: 'command',
			historyIndex: -1,
			historyDraft: undefined,
			tabPressCount: 0,
		};
	}

	return {
		...state,
		input: `${state.input}${char}`,
		historyIndex: -1,
		historyDraft: undefined,
		tabPressCount: 0,
	};
}

export function handleBackspace(state: AssistantInputState): AssistantInputState {
	if (state.input.length > 0) {
		const nextInput = state.input.slice(0, -1);
		if (state.mode === 'command' && nextInput.length === 0) {
			return {
				...state,
				mode: 'chat',
				input: '',
				historyIndex: -1,
				historyDraft: undefined,
				tabPressCount: 0,
			};
		}

		return {
			...state,
			input: nextInput,
			historyIndex: -1,
			historyDraft: undefined,
			tabPressCount: 0,
		};
	}

	if (state.mode === 'command') {
		return {
			...state,
			mode: 'chat',
			input: '',
			historyIndex: -1,
			historyDraft: undefined,
			tabPressCount: 0,
		};
	}

	return state;
}

export function handleEscape(state: AssistantInputState): AssistantInputState {
	if (state.mode !== 'command') {
		return state;
	}

	return {
		...state,
		mode: 'chat',
		input: '',
		historyIndex: -1,
		historyDraft: undefined,
		tabPressCount: 0,
	};
}

export function submitInput(state: AssistantInputState): SubmitResult {
	const hasText = state.input.trim().length > 0;
	if (!hasText) {
		return {
			state: {
				...state,
				tabPressCount: 0,
			},
		};
	}

	const submittedKind: SubmitKind = state.mode === 'command' ? 'command' : 'chat';
	const submittedText = submittedKind === 'command' ? `/${state.input.trim()}` : state.input;

	return {
		state: {
			mode: 'chat',
			input: '',
			history: [...state.history, submittedText],
			historyIndex: -1,
			historyDraft: undefined,
			tabPressCount: 0,
		},
		submittedText,
		submittedKind,
	};
}

export function handleHistoryUp(state: AssistantInputState): AssistantInputState {
	if (state.history.length === 0) {
		return state;
	}

	if (state.historyIndex < 0) {
		const restored = restoreHistoryEntry(state.history[state.history.length - 1]);
		return {
			...state,
			mode: restored.mode,
			input: restored.input,
			historyIndex: state.history.length - 1,
			historyDraft: {
				mode: state.mode,
				input: state.input,
			},
			tabPressCount: 0,
		};
	}

	if (state.historyIndex === 0) {
		return state;
	}

	const historyIndex = state.historyIndex - 1;
	const restored = restoreHistoryEntry(state.history[historyIndex]);
	return {
		...state,
		mode: restored.mode,
		input: restored.input,
		historyIndex,
		tabPressCount: 0,
	};
}

export function handleHistoryDown(state: AssistantInputState): AssistantInputState {
	if (state.history.length === 0 || state.historyIndex < 0) {
		return state;
	}

	const nextIndex = state.historyIndex + 1;
	if (nextIndex >= state.history.length) {
		const draft = state.historyDraft;
		return {
			...state,
			mode: draft?.mode ?? 'chat',
			input: draft?.input ?? '',
			historyIndex: -1,
			historyDraft: undefined,
			tabPressCount: 0,
		};
	}

	const restored = restoreHistoryEntry(state.history[nextIndex]);
	return {
		...state,
		mode: restored.mode,
		input: restored.input,
		historyIndex: nextIndex,
		tabPressCount: 0,
	};
}

export function handleCommandTab(state: AssistantInputState, commands: string[]): TabResult {
	if (state.mode !== 'command') {
		return { state };
	}

	const currentCommand = `/${state.input}`;
	const matches = commands.filter((command) => command.startsWith(currentCommand));

	if (matches.length === 1) {
		return {
			state: {
				...state,
				input: matches[0].slice(1),
				tabPressCount: 0,
			},
		};
	}

	if (state.tabPressCount === 1) {
		return {
			state: {
				...state,
				tabPressCount: 0,
			},
			listMatches: matches,
		};
	}

	return {
		state: {
			...state,
			tabPressCount: 1,
		},
	};
}

export type SlashCommandName =
	| 'help'
	| 'ask'
	| 'summarize'
	| 'review'
	| 'settings'
	| 'showreport'
	| 'exit'
	| 'unknown';

export interface ParsedSlashCommand {
	name: SlashCommandName;
	raw: string;
	arguments: string[];
	options: string[];
}

function tokenizeSlashCommand(input: string): string[] {
	const tokens: string[] = [];
	const tokenPattern = /"([^"]*)"|'([^']*)'|[^\s]+/g;

	for (const match of input.matchAll(tokenPattern)) {
		tokens.push(match[1] ?? match[2] ?? match[0]);
	}

	return tokens;
}

export function parseSlashCommand(input: string): ParsedSlashCommand {
	const raw = input.trim();
	const tokens = tokenizeSlashCommand(raw);
	const commandToken = tokens.shift() ?? '';

	const commandName = commandToken.startsWith('/')
		? commandToken.slice(1).toLowerCase()
		: commandToken.toLowerCase();

	const knownCommands: SlashCommandName[] = [
		'help',
		'ask',
		'summarize',
		'review',
		'settings',
		'showreport',
		'exit',
	];

	const name = knownCommands.includes(commandName as SlashCommandName)
		? commandName as SlashCommandName
		: 'unknown';

	return {
		name,
		raw,
		arguments: tokens.filter((token) => !token.startsWith('-')),
		options: tokens.filter((token) => token.startsWith('-')),
	};
}

export type AskRoute = 'auto' | 'summary' | 'knowledgebase' | 'chat';

export type AskCommandResolution =
	| {
		ok: true;
		route: AskRoute;
		question: string;
	}
	| {
		ok: false;
		error: string;
	};

const ASK_ROUTE_OPTIONS: Record<string, AskRoute> = {
	'--auto': 'auto',
	'-a': 'auto',
	'--summary': 'summary',
	'-s': 'summary',
	'--knowledgebase': 'knowledgebase',
	'-k': 'knowledgebase',
	'--chat': 'chat',
	'-c': 'chat',
};

export function resolveAskCommand(
	parsed: ParsedSlashCommand
): AskCommandResolution {
	const unknownOptions = parsed.options.filter(
		(option) => !(option in ASK_ROUTE_OPTIONS)
	);

	if (unknownOptions.length > 0) {
		return {
			ok: false,
			error: `Unknown /ask option: ${unknownOptions.join(', ')}`,
		};
	}

	const requestedRoutes = [
		...new Set(
			parsed.options.map((option) => ASK_ROUTE_OPTIONS[option])
		),
	];

	if (requestedRoutes.length > 1) {
		return {
			ok: false,
			error: 'Choose only one /ask route.',
		};
	}

	const question = parsed.arguments.join(' ').trim();
	if (!question) {
		return {
			ok: false,
			error: 'Usage: /ask [--auto | --summary | --knowledgebase | --chat] <question>',
		};
	}

	return {
		ok: true,
		route: requestedRoutes[0] ?? 'auto',
		question,
	};
}
