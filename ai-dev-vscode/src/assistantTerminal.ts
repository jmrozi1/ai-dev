import * as vscode from 'vscode';
import {
	applyTextInput,
	createAssistantInputState,
	getPromptMarker,
	handleBackspace,
	handleCommandTab,
	handleEscape,
	handleHistoryDown,
	handleHistoryUp,
	parseSlashCommand,
	submitInput,
	type AssistantInputState,
	type SubmitKind,
} from './assistantInput';
import {
	type AssistantChatBackend,
	VsCodeAssistantChatBackend,
} from './assistantChatBackend';

const ANSI_RESET = '\x1b[0m';
const ANSI_LIGHT_GRAY = '\x1b[90m';
const ANSI_CYAN = '\x1b[96m';
const ANSI_YELLOW = '\x1b[33m';
const ANSI_RED = '\x1b[31m';
const ANSI_WHITE = '\x1b[37m';
const ANSI_BG_DARK_GRAY = '\x1b[100m';
const ANSI_LAVENDER = '\x1b[38;2;238;238;255m';

const SEPARATOR_CHAR = '─';
const BULLET_CHAR = '•';
export const MODEL_RESPONSE_MARKER = '◆';
const SPINNER_FRAMES = ['◐', '◓', '◑', '◒'];

const ASSISTANT_TERMINAL_NAME = 'AI Dev';
const ASSISTANT_COMMANDS = ['/help', '/exit'];

export function createSeparatorLine(width: number): string {
	return SEPARATOR_CHAR.repeat(Math.max(1, width));
}

export function formatSubmittedInput(text: string, kind: SubmitKind): string {
	if (kind === 'command') {
		return `/ ${text.replace(/^\//, '')}`;
	}

	return `> ${text}`;
}

export function createFullWidthHighlightedRow(text: string, width: number): string {
	const safeWidth = Math.max(1, width);
	const visibleText = text.slice(0, safeWidth);
	const padding = ' '.repeat(Math.max(0, safeWidth - visibleText.length));
	return `${ANSI_WHITE}${ANSI_BG_DARK_GRAY}${visibleText}${padding}${ANSI_RESET}`;
}

export function getMatchingAssistantCommands(
	query: string,
	commands: string[] = ASSISTANT_COMMANDS
): string[] {
	const normalizedQuery = query.replace(/^\//, '').trim().toLowerCase();

	if (!normalizedQuery) {
		return [...commands];
	}

	return commands.filter((command) =>
		command.slice(1).toLowerCase().includes(normalizedQuery)
	);
}

export function formatCommandLookupLine(command: string, query: string): string {
	const commandName = command.replace(/^\//, '');
	const normalizedQuery = query.replace(/^\//, '').trim();
	const lowerName = commandName.toLowerCase();
	const lowerQuery = normalizedQuery.toLowerCase();

	if (!lowerQuery) {
		return `${ANSI_LAVENDER}/${ANSI_RESET}${ANSI_LIGHT_GRAY}${commandName}${ANSI_RESET}`;
	}

	const matchIndex = lowerName.indexOf(lowerQuery);
	if (matchIndex < 0) {
		return `${ANSI_LIGHT_GRAY}${command}${ANSI_RESET}`;
	}

	const before = commandName.slice(0, matchIndex);
	const match = commandName.slice(matchIndex, matchIndex + normalizedQuery.length);
	const after = commandName.slice(matchIndex + normalizedQuery.length);

	return [
		ANSI_LIGHT_GRAY,
		'/',
		before,
		ANSI_LAVENDER,
		match,
		ANSI_LIGHT_GRAY,
		after,
		ANSI_RESET,
	].join('');
}

export function formatModelResponseLines(
	modelName: string,
	responseText: string
): string[] {
	const normalized = responseText.replace(/\r\n/g, '\n').trim();

	if (!normalized) {
		return [];
	}

	return normalized.split('\n').map((line, index) =>
		index === 0
			? `${MODEL_RESPONSE_MARKER} ${modelName}: ${line}`
			: `  ${line}`
	);
}

type TerminalWindowApi = Pick<typeof vscode.window, 'createTerminal' | 'onDidCloseTerminal'>;

export class AiDevAssistantTerminalManager implements vscode.Disposable {
	private terminal: vscode.Terminal | undefined;
	private readonly closeSubscription: vscode.Disposable;

	constructor(
		private readonly windowApi: TerminalWindowApi,
		private readonly backendFactory: () => AssistantChatBackend =
			() => new VsCodeAssistantChatBackend()
	) {
		this.closeSubscription = this.windowApi.onDidCloseTerminal((closedTerminal) => {
			if (closedTerminal === this.terminal) {
				this.terminal = undefined;
			}
		});
	}

	launchAssistant(): vscode.Terminal {
		if (this.terminal) {
			this.terminal.show();
			return this.terminal;
		}

		const pty = new AiDevAssistantPseudoterminal(
			() => {
				this.terminal?.dispose();
			},
			this.backendFactory()
		);

		const terminal = this.windowApi.createTerminal({
			name: ASSISTANT_TERMINAL_NAME,
			pty,
		});

		this.terminal = terminal;
		terminal.show();
		return terminal;
	}

	hasActiveTerminal(): boolean {
		return this.terminal !== undefined;
	}

	dispose(): void {
		this.closeSubscription.dispose();
		this.terminal = undefined;
	}
}

export class AiDevAssistantPseudoterminal implements vscode.Pseudoterminal {
	private readonly writeEmitter = new vscode.EventEmitter<string>();
	private readonly closeEmitter = new vscode.EventEmitter<void>();

	private state: AssistantInputState = createAssistantInputState();
	private spinnerTimer: NodeJS.Timeout | undefined;
	private requestCancellation: vscode.CancellationTokenSource | undefined;

	private spinnerFrameIndex = 0;
	private ephemeralVisible = false;
	private promptVisible = false;
	private commandLookupLineCount = 0;
	private requestInFlight = false;
	private isReady = false;
	private modelName = 'AI Dev';
	private width = 40;

	readonly onDidWrite = this.writeEmitter.event;
	readonly onDidClose = this.closeEmitter.event;

	constructor(
		private readonly onExitRequested: () => void,
		private readonly chatBackend: AssistantChatBackend =
			new VsCodeAssistantChatBackend()
	) {}

	open(initialDimensions: vscode.TerminalDimensions | undefined): void {
		if (initialDimensions?.columns && initialDimensions.columns > 0) {
			this.width = initialDimensions.columns;
		}

		void this.startup();
	}

	close(): void {
		this.stopSpinner();
		this.requestCancellation?.cancel();
		this.requestCancellation?.dispose();
		this.requestCancellation = undefined;
		this.requestInFlight = false;
		this.chatBackend.dispose();
		this.closeEmitter.fire();
	}

	setDimensions(dimensions: vscode.TerminalDimensions): void {
		if (dimensions.columns > 0) {
			this.width = dimensions.columns;
		}
	}

	handleInput(data: string): void {
		try {
			if (data === '\x03') {
				this.handleCtrlC();
				return;
			}

			if (!this.isReady || this.requestInFlight) {
				return;
			}

			switch (data) {
				case '\r':
					void this.handleEnter();
					return;

				case '\x7f':
					this.state = handleBackspace(this.state);
					this.refreshInteractiveArea();
					return;

				case '\x1b':
					this.state = handleEscape(this.state);
					this.refreshInteractiveArea();
					return;

				case '\x1b[A':
					this.state = handleHistoryUp(this.state);
					this.refreshInteractiveArea();
					return;

				case '\x1b[B':
					this.state = handleHistoryDown(this.state);
					this.refreshInteractiveArea();
					return;

				case '\t':
					this.handleTab();
					return;

				default:
					break;
			}

			const printable = [...data]
				.filter((char) => char >= ' ' && char !== '\x7f')
				.join('');

			if (!printable) {
				return;
			}

			this.state = applyTextInput(this.state, printable);
			this.refreshInteractiveArea();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			this.prepareForPermanentOutput();
			this.writePermanentLine(`ERROR Assistant input failed: ${message}`, ANSI_RED);
			this.drawInteractiveArea();
		}
	}

	private async startup(): Promise<void> {
		const cancellation = new vscode.CancellationTokenSource();
		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.startSpinner('Selecting language model...');

		try {
			const session = await this.chatBackend.startSession(cancellation.token);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant startup cancelled.');
			}

			this.modelName = session.modelName;
			this.startSpinner(`Starting ${this.modelName}...`);

			await new Promise<void>((resolve) => setTimeout(resolve, 200));

			this.stopSpinner();
			this.clearStandaloneEphemeralLine();
			this.writePermanentLine(
				`${BULLET_CHAR} Launched ${this.modelName}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Type /help for commands`,
				ANSI_LIGHT_GRAY
			);
		} catch (error) {
			this.stopSpinner();
			this.clearStandaloneEphemeralLine();

			const message = error instanceof Error ? error.message : String(error);
			this.writePermanentLine(`ERROR ${message}`, ANSI_RED);
		} finally {
			cancellation.dispose();

			if (this.requestCancellation === cancellation) {
				this.requestCancellation = undefined;
			}

			this.requestInFlight = false;
			this.isReady = true;
			this.drawInteractiveArea();
		}
	}

	private async handleEnter(): Promise<void> {
		const submitResult = submitInput(this.state);
		this.state = submitResult.state;

		if (!submitResult.submittedText || !submitResult.submittedKind) {
			this.renderInputLine();
			return;
		}

		this.clearInteractiveArea();

		const submittedDisplay = formatSubmittedInput(
			submitResult.submittedText,
			submitResult.submittedKind
		);

		this.writeHighlightedRow(submittedDisplay);
		this.writeBlankLine();

		if (submitResult.submittedKind === 'command') {
			const shouldRedraw = this.handleSubmittedCommand(
				submitResult.submittedText
			);

			if (shouldRedraw) {
				this.drawInteractiveArea();
			}

			return;
		}

		const cancellation = new vscode.CancellationTokenSource();
		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.showEphemeralWithPrompt(`Thinking with ${this.modelName}...`);

		try {
			const responseText = await this.chatBackend.sendMessage(
				submitResult.submittedText,
				cancellation.token
			);

			this.prepareForPermanentOutput();

			if (cancellation.token.isCancellationRequested) {
				this.writePermanentLine(
					`${BULLET_CHAR} Cancelled`,
					ANSI_LIGHT_GRAY
				);
			} else if (!responseText.trim()) {
				this.writePermanentLine(
					'WARNING The model returned an empty response.',
					ANSI_YELLOW
				);
			} else {
				for (const line of formatModelResponseLines(
					this.modelName,
					responseText
				)) {
					this.writePermanentLine(line, ANSI_LIGHT_GRAY);
				}
			}
		} catch (error) {
			this.prepareForPermanentOutput();

			if (cancellation.token.isCancellationRequested) {
				this.writePermanentLine(
					`${BULLET_CHAR} Cancelled`,
					ANSI_LIGHT_GRAY
				);
			} else {
				const message =
					error instanceof Error ? error.message : String(error);

				this.writePermanentLine(
					`ERROR Model request failed: ${message}`,
					ANSI_RED
				);
			}
		} finally {
			cancellation.dispose();

			if (this.requestCancellation === cancellation) {
				this.requestCancellation = undefined;
			}

			this.requestInFlight = false;
			this.drawInteractiveArea();
		}
	}

	private handleSubmittedCommand(command: string): boolean {
		switch (parseSlashCommand(command)) {
			case 'help':
				this.writePermanentLine(
					`${BULLET_CHAR} Available commands: /help, /exit`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Tab completes commands`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Tab twice lists matching commands`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Escape leaves command mode`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Up/Down navigate history`,
					ANSI_LIGHT_GRAY
				);
				return true;

			case 'exit':
				this.writePermanentLine(
					`${BULLET_CHAR} Exiting AI Dev`,
					ANSI_LIGHT_GRAY
				);
				this.onExitRequested();
				return false;

			case 'unknown':
			default:
				this.writePermanentLine(
					`WARNING Unknown command: ${command}`,
					ANSI_YELLOW
				);
				return true;
		}
	}

	private handleTab(): void {
		const tabResult = handleCommandTab(this.state, ASSISTANT_COMMANDS);
		this.state = tabResult.state;

		if (!tabResult.listMatches) {
			this.refreshInteractiveArea();
			return;
		}

		this.clearInteractiveArea();

		if (tabResult.listMatches.length === 0) {
			this.writePermanentLine('WARNING No matching commands.', ANSI_YELLOW);
		} else {
			this.writePermanentLine(
				`${BULLET_CHAR} ${tabResult.listMatches.join('  ')}`,
				ANSI_LIGHT_GRAY
			);
		}

		this.drawInteractiveArea();
	}

	private handleCtrlC(): void {
		if (this.requestInFlight && this.requestCancellation) {
			this.requestCancellation.cancel();
			return;
		}

		this.renderInputLine();
	}

	private refreshInteractiveArea(): void {
		if (this.state.mode === 'command' || this.commandLookupLineCount > 0) {
			this.clearInteractiveArea();
			this.drawInteractiveArea();
			return;
		}

		this.renderInputLine();
	}

	private drawInteractiveArea(): void {
		this.drawPromptArea();

		if (this.state.mode === 'command') {
			this.drawCommandLookupBelowPrompt();
		}
	}

	private drawCommandLookupBelowPrompt(): void {
		const matches = getMatchingAssistantCommands(this.state.input);
		this.commandLookupLineCount = matches.length;

		if (matches.length === 0) {
			return;
		}

		// Preserve the editable cursor, move below the bottom rule,
		// render ephemeral command matches, then restore the cursor.
		this.writeRaw('\x1b7');
		this.writeRaw('\x1b[2B\r');

		for (const command of matches) {
			this.writeRaw(`${formatCommandLookupLine(command, this.state.input)}\r\n`);
		}

		this.writeRaw('\x1b8');
	}

	private clearInteractiveArea(): void {
		if (this.commandLookupLineCount > 0 && this.promptVisible) {
			// Remove command suggestions below the prompt without moving
			// the editable cursor permanently.
			this.writeRaw('\x1b7');
			this.writeRaw('\x1b[2B\r');
			this.writeRaw(`\x1b[${this.commandLookupLineCount}M`);
			this.writeRaw('\x1b8');
			this.commandLookupLineCount = 0;
		}

		this.clearPromptArea();
	}

	private drawPromptArea(): void {
		if (this.promptVisible) {
			this.renderInputLine();
			return;
		}

		const separator = createSeparatorLine(this.width);
		const inputLine = `${getPromptMarker(this.state)} ${this.state.input}`;

		this.writeRaw(`${separator}\r\n`);
		this.writeRaw(`${inputLine}\r\n`);
		this.writeRaw(separator);

		this.writeRaw('\x1b[1A\r');
		this.moveCursorToInputEnd();

		this.promptVisible = true;
	}

	private clearPromptArea(): void {
		if (!this.promptVisible) {
			return;
		}

		this.writeRaw('\r\x1b[1A\x1b[3M');
		this.promptVisible = false;
	}

	private renderInputLine(): void {
		if (!this.promptVisible) {
			this.drawInteractiveArea();
			return;
		}

		const inputLine = `${getPromptMarker(this.state)} ${this.state.input}`;
		this.writeRaw(`\r\x1b[2K${inputLine}`);
	}

	private moveCursorToInputEnd(): void {
		const column = `${getPromptMarker(this.state)} ${this.state.input}`.length;
		if (column > 0) {
			this.writeRaw(`\x1b[${column}C`);
		}
	}

	private writeHighlightedRow(text: string): void {
		this.writeRaw(`${createFullWidthHighlightedRow(text, this.width)}\r\n`);
	}

	private writeBlankLine(): void {
		this.writeRaw('\r\n');
	}

	private writePermanentLine(text: string, color: string): void {
		this.writeRaw(`${color}${text}${ANSI_RESET}\r\n`);
	}

	private showEphemeralWithPrompt(label: string): void {
		this.ephemeralVisible = true;
		this.spinnerFrameIndex = 0;
		this.writeRaw(
			`${ANSI_CYAN}${SPINNER_FRAMES[this.spinnerFrameIndex]} ${label}${ANSI_RESET}\r\n`
		);
		this.drawInteractiveArea();

		this.stopSpinner();
		this.spinnerTimer = setInterval(() => {
			this.spinnerFrameIndex =
				(this.spinnerFrameIndex + 1) % SPINNER_FRAMES.length;
			this.updateEphemeralAboveInteractiveArea(label);
		}, 120);
	}

	private updateEphemeralAboveInteractiveArea(label: string): void {
		if (!this.ephemeralVisible || !this.promptVisible) {
			return;
		}

		const rowsAbovePromptMiddle = 2;

		this.writeRaw('\x1b7');
		this.writeRaw(
			`\x1b[${rowsAbovePromptMiddle}A\r\x1b[2K${ANSI_CYAN}${SPINNER_FRAMES[this.spinnerFrameIndex]} ${label}${ANSI_RESET}`
		);
		this.writeRaw('\x1b8');
	}

	private prepareForPermanentOutput(): void {
		this.stopSpinner();
		this.clearInteractiveArea();

		if (this.ephemeralVisible) {
			this.writeRaw('\x1b[1A\r\x1b[2K');
			this.ephemeralVisible = false;
		}
	}

	private startSpinner(label: string): void {
		this.stopSpinner();
		this.spinnerFrameIndex = 0;
		this.ephemeralVisible = true;

		this.writeRaw(
			`${ANSI_CYAN}${SPINNER_FRAMES[this.spinnerFrameIndex]} ${label}${ANSI_RESET}`
		);

		this.spinnerTimer = setInterval(() => {
			this.spinnerFrameIndex =
				(this.spinnerFrameIndex + 1) % SPINNER_FRAMES.length;
			this.writeRaw(
				`\r\x1b[2K${ANSI_CYAN}${SPINNER_FRAMES[this.spinnerFrameIndex]} ${label}${ANSI_RESET}`
			);
		}, 120);
	}

	private clearStandaloneEphemeralLine(): void {
		if (!this.ephemeralVisible) {
			return;
		}

		this.writeRaw('\r\x1b[2K');
		this.ephemeralVisible = false;
	}

	private stopSpinner(): void {
		if (!this.spinnerTimer) {
			return;
		}

		clearInterval(this.spinnerTimer);
		this.spinnerTimer = undefined;
	}

	private writeRaw(data: string): void {
		this.writeEmitter.fire(data);
	}
}

export const AI_DEV_ASSISTANT_TERMINAL_NAME = ASSISTANT_TERMINAL_NAME;
