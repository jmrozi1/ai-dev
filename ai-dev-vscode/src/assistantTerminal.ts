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
	resolveAskCommand,
	submitInput,
	type AssistantInputState,
	type SubmitKind,
} from './assistantInput';
import {
	type AssistantChatBackend,
	VsCodeAssistantChatBackend,
} from './assistantChatBackend';
import {
	createAssistantReport,
	parseReviewFindings,
	type AssistantReport,
} from './assistantReport';
import {
	getAssistantCommandDefinition,
	getAssistantCommandNames,
	formatAssistantCommandHelp,
	getAssistantLookupItems,
	type AssistantLookupItem,
} from './assistantCommands';
import {
	chooseAutomaticAssistantRoute,
} from './assistantRouting';

const ANSI_RESET = '\x1b[0m';
const ANSI_LIGHT_GRAY = '\x1b[90m';
const ANSI_CYAN = '\x1b[96m';
const ANSI_YELLOW = '\x1b[33m';
const ANSI_RED = '\x1b[31m';
const ANSI_WHITE = '\x1b[37m';
const ANSI_BRIGHT_WHITE = '\x1b[97m';
const ANSI_COMPLETION = '\x1b[38;2;156;220;254m';
const ANSI_BG_DARK_GRAY = '\x1b[100m';
const ANSI_LAVENDER = '\x1b[38;2;221;221;255m';

const SEPARATOR_CHAR = '─';
const BULLET_CHAR = '•';
export const MODEL_RESPONSE_MARKER = '◆';
const SPINNER_FRAMES = ['◐', '◓', '◑', '◒'];

const ASSISTANT_TERMINAL_NAME = 'AI Dev';
const ASSISTANT_COMMANDS = getAssistantCommandNames();

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
	const commandInput = query.replace(/^\//, '');

	if (/\s/.test(commandInput)) {
		return [];
	}

	const normalizedQuery = commandInput.trim().toLowerCase();

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

export function formatOptionLookupLine(
	display: string,
	query: string
): string {
	const activeToken = query.trim().split(/\s+/).pop() ?? '';
	const normalizedQuery = activeToken.toLowerCase();

	if (!normalizedQuery || normalizedQuery === '-') {
		return `${ANSI_LIGHT_GRAY}${display}${ANSI_RESET}`;
	}

	const lowerDisplay = display.toLowerCase();
	const matchIndex = lowerDisplay.indexOf(normalizedQuery);

	if (matchIndex < 0) {
		return `${ANSI_LIGHT_GRAY}${display}${ANSI_RESET}`;
	}

	const before = display.slice(0, matchIndex);
	const match = display.slice(
		matchIndex,
		matchIndex + activeToken.length
	);
	const after = display.slice(matchIndex + activeToken.length);

	return [
		ANSI_LIGHT_GRAY,
		before,
		ANSI_LAVENDER,
		match,
		ANSI_LIGHT_GRAY,
		after,
		ANSI_RESET,
	].join('');
}

export function getCommonPrefix(values: string[]): string {
	if (values.length === 0) {
		return '';
	}

	let prefix = values[0];

	for (const value of values.slice(1)) {
		while (prefix && !value.startsWith(prefix)) {
			prefix = prefix.slice(0, -1);
		}
	}

	return prefix;
}

export function formatItemsInColumns(
	items: string[],
	width: number
): string[] {
	if (items.length === 0) {
		return [];
	}

	const longest = Math.max(
		...items.map((item) => item.length)
	);
	const columnWidth = longest + 2;
	const columnCount = Math.max(
		1,
		Math.floor(Math.max(1, width) / columnWidth)
	);

	const lines: string[] = [];

	for (
		let index = 0;
		index < items.length;
		index += columnCount
	) {
		const row = items.slice(
			index,
			index + columnCount
		);

		lines.push(
			row
				.map((item, itemIndex) =>
					itemIndex === row.length - 1
						? item
						: item.padEnd(columnWidth)
				)
				.join('')
				.trimEnd()
		);
	}

	return lines;
}

export interface PathCompletionContext {
	partialPath: string;
	beforePath: string;
	quote: '"' | "'" | '';
}

export function getPathCompletionContext(
	input: string
): PathCompletionContext | undefined {
	const match = input.match(
		/^(\s*summarize\s+)(["']?)([^"']*)$/
	);

	if (!match) {
		return undefined;
	}

	const beforePath = match[1];
	const quote = (match[2] ?? '') as '"' | "'" | '';
	const partialPath = match[3] ?? '';

	if (
		partialPath.startsWith('-')
		|| /[*?\[\]{}]/.test(partialPath)
	) {
		return undefined;
	}

	return {
		partialPath,
		beforePath,
		quote,
	};
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
			? `${ANSI_LAVENDER}${MODEL_RESPONSE_MARKER} ${modelName}:${ANSI_RESET} ${ANSI_BRIGHT_WHITE}${line}${ANSI_RESET}`
			: `${ANSI_BRIGHT_WHITE}  ${line}${ANSI_RESET}`
	);
}

export interface AssistantSummaryRouteResult {
	prompt: string;
	warnings: string[];
}

export type AssistantSummaryRoute = (
	question: string
) => Promise<AssistantSummaryRouteResult>;

export interface AssistantSummarizePreparation {
	prompt: string;
	sourcePath: string;
	outputPath: string;
	warnings: string[];
}

export interface AssistantSummarizeCompletion {
	written: boolean;
	outputPath: string;
	warnings: string[];
}

export interface AssistantSummarizePreview {
	target: string;
	matchedSourceCount: number;
	plannedSummaryTargets: string[];
	previewSourcePaths: string[];
	omittedSourceCount: number;
	warnings: string[];
}

export interface AssistantSummarizeExecutionProgress {
	completedModelCalls: number;
	totalModelCalls: number;
	outputPath: string;
}

export interface AssistantSummarizeExecutionResult {
	matchedSourceCount: number;
	plannedModelCalls: number;
	completedModelCalls: number;
	updatedSummaryPaths: string[];
	architectureSummaryPath?: string;
	architectureUpdated: boolean;
	architectureSkipped?: string;
	architectureFailed?: string;
	dependencyFilesIncluded: number;
	dependencyWarnings: string[];
	skipped: string[];
	failed: string[];
	cancelled: boolean;
}

export interface AssistantSummarizeExecutionOptions {
	cancellationToken: vscode.CancellationToken;
	sendPrompt(
		prompt: string,
		cancellationToken: vscode.CancellationToken
	): Promise<string>;
	onProgress(
		progress: AssistantSummarizeExecutionProgress
	): void;
}

export interface AssistantSummarizeRoute {
	preview(
		target: string
	): Promise<AssistantSummarizePreview>;
	execute(
		target: string,
		options: AssistantSummarizeExecutionOptions
	): Promise<AssistantSummarizeExecutionResult>;
	prepare(
		target: string
	): Promise<AssistantSummarizePreparation>;
	complete(
		preparation: AssistantSummarizePreparation,
		responseText: string
	): Promise<AssistantSummarizeCompletion>;
}

export type AssistantReviewMode =
	| 'docs'
	| 'code'
	| 'tests';

export interface AssistantReviewPreparation {
	mode: AssistantReviewMode;
	prompt: string;
	changedFileCount: number;
	deterministicFindingCount: number;
	deterministicFindingsMarkdown: string;
	warnings: string[];
}

export interface AssistantReviewRequest {
	mode: AssistantReviewMode;
	target?: string;
	includeAllMatches: boolean;
	smokeTest: boolean;
}

export interface AssistantReviewPreview {
	mode: AssistantReviewMode;
	target?: string;
	includeAllMatches: boolean;
	implementationFileCount: number;
	testFileCount: number;
	selectedFileCount: number;
	changedFileCount: number;
	previewFilePaths: string[];
	omittedFileCount: number;
	warnings: string[];
}

export interface AssistantReviewRoute {
	preview(
		request: AssistantReviewRequest
	): Promise<AssistantReviewPreview>;
	prepare(
		request: AssistantReviewRequest
	): Promise<AssistantReviewPreparation>;
}

export type ReviewModeResolution =
	| {
		ok: true;
		mode: AssistantReviewMode;
	}
	| {
		ok: false;
		error: string;
	};

export function resolveReviewMode(
	options: string[]
): ReviewModeResolution {
	const supportedModeOptions = [
		'--docs',
		'-d',
		'--code',
		'-c',
		'--tests',
		'-t',
		'--all',
		'-a',
		'--smoketest',
		'-s',
	];

	const unsupportedOptions = options.filter(
		(option) => !supportedModeOptions.includes(option)
	);

	if (unsupportedOptions.length > 0) {
		return {
			ok: false,
			error:
				`Unknown /review option: ${unsupportedOptions.join(', ')}`,
		};
	}

	const requestedModes: AssistantReviewMode[] = [];

	if (
		options.includes('--docs')
		|| options.includes('-d')
	) {
		requestedModes.push('docs');
	}

	if (
		options.includes('--code')
		|| options.includes('-c')
	) {
		requestedModes.push('code');
	}

	if (
		options.includes('--tests')
		|| options.includes('-t')
	) {
		requestedModes.push('tests');
	}

	if (requestedModes.length > 1) {
		return {
			ok: false,
			error:
				'Choose only one review mode: --docs, --code, or --tests.',
		};
	}

	return {
		ok: true,
		mode: requestedModes[0] ?? 'docs',
	};
}

export type ReviewRequestResolution =
	| {
		ok: true;
		request: AssistantReviewRequest;
	}
	| {
		ok: false;
		error: string;
	};

export function resolveReviewRequest(
	options: string[],
	arguments_: string[]
): ReviewRequestResolution {
	const modeResolution = resolveReviewMode(options);

	if (!modeResolution.ok) {
		return modeResolution;
	}

	const target = arguments_.join(' ').trim() || undefined;

	return {
		ok: true,
		request: {
			mode: modeResolution.mode,
			target,
			includeAllMatches:
				options.includes('--all')
				|| options.includes('-a'),
			smokeTest:
				options.includes('--smoketest')
				|| options.includes('-s'),
		},
	};
}

export function buildUnstructuredReviewFallback(
	mode: AssistantReviewMode
): string {
	const category =
		mode === 'tests'
			? 'Test coverage'
			: mode === 'code'
				? 'Reliability'
				: 'Uncertainty';

	return [
		'## Finding: Review returned no structured assessment',
		'',
		'**Severity:** warning',
		'',
		`**Category:** ${category}`,
		'',
		'**Source file:**',
		'`none`',
		'',
		'**Documentation file:**',
		'`none`',
		'',
		'### Evidence',
		'',
		'- The model response did not follow the required structured finding template.',
		'- The supplied review context could not be converted into actionable findings.',
		'',
		'### Impact',
		'',
		'The review result cannot be reliably sorted, inspected, or acted upon.',
		'',
		'### Suggested action',
		'',
		'Retry the review. If the response remains unstructured, inspect the prompt and model availability.',
		'',
		'### AI-generated update appropriate?',
		'',
		'No.',
		'',
		'This finding describes a failed review response rather than a source change.',
		'',
		'### Uncertainty',
		'',
		'The model may have ignored the review instructions or lacked sufficient usable context.',
	].join('\n');
}


export type AssistantReportSink = (
	report: AssistantReport
) => void;

export type AssistantReportOpener = () => boolean;

export type AssistantSummarizationConfigOpener =
	() => Promise<void>;

export interface AssistantPathCompletionResult {
	matches: string[];
}

export type AssistantPathCompleter = (
	partialPath: string
) => Promise<AssistantPathCompletionResult>;

type TerminalWindowApi = Pick<typeof vscode.window, 'createTerminal' | 'onDidCloseTerminal'>;

export class AiDevAssistantTerminalManager implements vscode.Disposable {
	private terminal: vscode.Terminal | undefined;
	private readonly closeSubscription: vscode.Disposable;

	constructor(
		private readonly windowApi: TerminalWindowApi,
		private readonly backendFactory: () => AssistantChatBackend =
			() => new VsCodeAssistantChatBackend(),
		private readonly summaryRoute?: AssistantSummaryRoute,
		private readonly summarizeRoute?: AssistantSummarizeRoute,
		private readonly reviewRoute?: AssistantReviewRoute,
		private readonly reportSink?: AssistantReportSink,
		private readonly reportOpener?: AssistantReportOpener,
		private readonly summarizationConfigOpener?:
			AssistantSummarizationConfigOpener,
		private readonly pathCompleter?: AssistantPathCompleter
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
			this.backendFactory(),
			this.summaryRoute,
			this.summarizeRoute,
			this.reviewRoute,
			this.reportSink,
			this.reportOpener,
			this.summarizationConfigOpener,
			this.pathCompleter
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
	private tabCompletionLineCount = 0;
	private requestInFlight = false;
	private isReady = false;
	private modelName = 'AI Dev';
	private width = 40;

	readonly onDidWrite = this.writeEmitter.event;
	readonly onDidClose = this.closeEmitter.event;

	constructor(
		private readonly onExitRequested: () => void,
		private readonly chatBackend: AssistantChatBackend =
			new VsCodeAssistantChatBackend(),
		private readonly summaryRoute?: AssistantSummaryRoute,
		private readonly summarizeRoute?: AssistantSummarizeRoute,
		private readonly reviewRoute?: AssistantReviewRoute,
		private readonly reportSink?: AssistantReportSink,
		private readonly reportOpener?: AssistantReportOpener,
		private readonly summarizationConfigOpener?:
			AssistantSummarizationConfigOpener,
		private readonly pathCompleter?: AssistantPathCompleter
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
					void this.handleTab();
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
				`${BULLET_CHAR} Type / for commands · Esc returns to chat`,
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
			const shouldRedraw = await this.handleSubmittedCommand(
				submitResult.submittedText
			);

			if (shouldRedraw) {
				this.drawInteractiveArea();
			}

			return;
		}

		await this.submitAutomaticPrompt(submitResult.submittedText);
	}

	private async runSummarizeSmokeTest(
		target: string
	): Promise<void> {
		this.showEphemeralWithPrompt(
			`Resolving ${target}...`
		);

		try {
			const preview =
				await this.summarizeRoute!.preview(target);

			this.prepareForPermanentOutput();

			for (const warning of preview.warnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			this.writePermanentLine(
				`${BULLET_CHAR} Matched source files: ${preview.matchedSourceCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Planned summary targets: ${preview.plannedSummaryTargets.length}`,
				ANSI_LIGHT_GRAY
			);
			const estimatedModelCalls =
				preview.plannedSummaryTargets.length
				+ (
					preview.plannedSummaryTargets.length > 0
						? 1
						: 0
				);

			this.writePermanentLine(
				`${BULLET_CHAR} Estimated model calls: ${estimatedModelCalls}`,
				ANSI_LIGHT_GRAY
			);
			if (preview.plannedSummaryTargets.length > 0) {
				this.writePermanentLine(
					`${BULLET_CHAR} Architecture refresh: planned`,
					ANSI_LIGHT_GRAY
				);
			}

			if (preview.previewSourcePaths.length > 0) {
				this.writePermanentLine(
					`${BULLET_CHAR} First ${preview.previewSourcePaths.length} matched files:`,
					ANSI_LIGHT_GRAY
				);

				for (const sourcePath of preview.previewSourcePaths) {
					this.writePermanentLine(
						`  ${sourcePath}`,
						ANSI_LIGHT_GRAY
					);
				}
			}

			if (preview.omittedSourceCount > 0) {
				this.writePermanentLine(
					`${BULLET_CHAR} ${preview.omittedSourceCount} additional files omitted`,
					ANSI_LIGHT_GRAY
				);
			}
		} catch (error) {
			this.prepareForPermanentOutput();

			const message =
				error instanceof Error
					? error.message
					: String(error);

			this.writePermanentLine(
				`ERROR Summarization smoke test failed: ${message}`,
				ANSI_RED
			);
		} finally {
			this.drawInteractiveArea();
		}
	}

	private async submitBatchSummarize(
		target: string
	): Promise<void> {
		const cancellation =
			new vscode.CancellationTokenSource();

		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.showEphemeralWithPrompt(
			`Planning summaries for ${target}...`
		);

		try {
			const result =
				await this.summarizeRoute!.execute(
					target,
					{
						cancellationToken: cancellation.token,
						sendPrompt: (
							prompt,
							cancellationToken
						) =>
							this.chatBackend.sendIsolatedMessage(
								prompt,
								cancellationToken
							),
						onProgress: (progress) => {
							this.showEphemeralWithPrompt(
								`${progress.completedModelCalls + 1}/${progress.totalModelCalls} Updating ${progress.outputPath}...`
							);
						},
					}
				);

			this.prepareForPermanentOutput();

			for (const warning of result.dependencyWarnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			for (const skipped of result.skipped) {
				this.writePermanentLine(
					`WARNING Skipped ${skipped}`,
					ANSI_YELLOW
				);
			}

			for (const failed of result.failed) {
				this.writePermanentLine(
					`ERROR ${failed}`,
					ANSI_RED
				);
			}

			for (
				let index = 0;
				index < result.updatedSummaryPaths.length;
				index += 1
			) {
				this.writePermanentLine(
					`${BULLET_CHAR} ${index + 1}/${result.plannedModelCalls} Updated ${result.updatedSummaryPaths[index]}`,
					ANSI_LIGHT_GRAY
				);
			}

			if (
				result.architectureUpdated
				&& result.architectureSummaryPath
			) {
				this.writePermanentLine(
					`${BULLET_CHAR} Updated architecture summary: ${result.architectureSummaryPath}`,
					ANSI_LIGHT_GRAY
				);
			} else if (result.architectureFailed) {
				this.writePermanentLine(
					`ERROR Architecture refresh failed: ${result.architectureFailed}`,
					ANSI_RED
				);
			} else if (result.architectureSkipped) {
				this.writePermanentLine(
					`WARNING Architecture refresh skipped: ${result.architectureSkipped}`,
					ANSI_YELLOW
				);
			}

			this.writePermanentLine(
				`${BULLET_CHAR} Updated summaries: ${result.updatedSummaryPaths.length}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Source files summarized: ${result.matchedSourceCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Dependency files included: ${result.dependencyFilesIncluded}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Model calls completed: ${result.completedModelCalls}/${result.plannedModelCalls}`,
				ANSI_LIGHT_GRAY
			);

			if (result.cancelled) {
				this.writePermanentLine(
					`${BULLET_CHAR} Cancelled`,
					ANSI_LIGHT_GRAY
				);
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
					error instanceof Error
						? error.message
						: String(error);

				this.writePermanentLine(
					`ERROR Batch summarization failed: ${message}`,
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

	private async submitSummarizePrompt(
		target: string
	): Promise<void> {
		const cancellation = new vscode.CancellationTokenSource();
		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.showEphemeralWithPrompt(
			`Preparing summary for ${target}...`
		);

		try {
			const preparation =
				await this.summarizeRoute!.prepare(target);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant request cancelled.');
			}

			this.showEphemeralWithPrompt(
				`Summarizing ${preparation.sourcePath}...`
			);

			const responseText =
				await this.chatBackend.sendIsolatedMessage(
					preparation.prompt,
					cancellation.token
				);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant request cancelled.');
			}

			this.showEphemeralWithPrompt(
				`Finalizing ${preparation.outputPath}...`
			);

			const completion =
				await this.summarizeRoute!.complete(
					preparation,
					responseText
				);

			this.prepareForPermanentOutput();

			const warnings = [
				...preparation.warnings,
				...completion.warnings,
			];

			for (const warning of warnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			if (completion.written) {
				this.writePermanentLine(
					`${BULLET_CHAR} Updated ${completion.outputPath}`,
					ANSI_LIGHT_GRAY
				);
			} else {
				this.writePermanentLine(
					`${BULLET_CHAR} Previewed ${completion.outputPath}; no file written`,
					ANSI_LIGHT_GRAY
				);
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
					error instanceof Error
						? error.message
						: String(error);

				this.writePermanentLine(
					`ERROR Summarization failed: ${message}`,
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

	private async runReviewSmokeTest(
		request: AssistantReviewRequest
	): Promise<void> {
		const targetLabel =
			request.target
				? request.target
				: 'entire project';

		this.showEphemeralWithPrompt(
			`Resolving review scope for ${targetLabel}...`
		);

		try {
			const preview =
				await this.reviewRoute!.preview(request);

			this.prepareForPermanentOutput();

			for (const warning of preview.warnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			this.writePermanentLine(
				`${BULLET_CHAR} Review mode: ${preview.mode}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Candidate source: ${
					preview.includeAllMatches
						? 'all project files'
						: 'changed files'
				}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Target: ${
					preview.target ?? 'entire project'
				}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Implementation files: ${preview.implementationFileCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Test files: ${preview.testFileCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Total files selected: ${preview.selectedFileCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Changed files in scope: ${preview.changedFileCount}`,
				ANSI_LIGHT_GRAY
			);

			if (preview.previewFilePaths.length > 0) {
				this.writePermanentLine(
					`${BULLET_CHAR} First ${preview.previewFilePaths.length} selected files:`,
					ANSI_LIGHT_GRAY
				);

				for (const filePath of preview.previewFilePaths) {
					this.writePermanentLine(
						`  ${filePath}`,
						ANSI_LIGHT_GRAY
					);
				}
			}

			if (preview.omittedFileCount > 0) {
				this.writePermanentLine(
					`${BULLET_CHAR} ${preview.omittedFileCount} additional files omitted`,
					ANSI_LIGHT_GRAY
				);
			}
		} catch (error) {
			this.prepareForPermanentOutput();

			const message =
				error instanceof Error
					? error.message
					: String(error);

			this.writePermanentLine(
				`ERROR Review smoke test failed: ${message}`,
				ANSI_RED
			);
		} finally {
			this.drawInteractiveArea();
		}
	}

	private async submitReview(
		request: AssistantReviewRequest
	): Promise<void> {
		const {
			mode,
			target,
			includeAllMatches,
		} = request;

		const cancellation =
			new vscode.CancellationTokenSource();

		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		const modeLabel =
			mode === 'docs'
				? 'documentation'
				: mode === 'code'
					? 'code'
					: 'test coverage';

		this.showEphemeralWithPrompt(
			`Preparing changed-${modeLabel} review...`
		);

		try {
			const preparation =
				await this.reviewRoute!.prepare(request);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant request cancelled.');
			}

			this.showEphemeralWithPrompt(
				`Reviewing ${preparation.changedFileCount} changed file(s) for ${modeLabel} issues...`
			);

			const modelResponse =
				await this.chatBackend.sendIsolatedMessage(
					preparation.prompt,
					cancellation.token
				);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant request cancelled.');
			}

			const parsedModelFindings =
				parseReviewFindings(modelResponse);

			const modelResponseIsStructured =
				parsedModelFindings.length > 0;

			const modelFindings =
				modelResponseIsStructured
					? modelResponse
					: buildUnstructuredReviewFallback(
						preparation.mode
					);

			const warnings = [...preparation.warnings];

			if (!modelResponse.trim()) {
				warnings.push(
					'The model returned no review findings.'
				);
			} else if (!modelResponseIsStructured) {
				warnings.push(
					'The model returned an unstructured review response; a fallback finding was created.'
				);
			}

			const combinedFindings = [
				preparation.deterministicFindingsMarkdown,
				'',
				'## Model Review Findings',
				'',
				modelFindings,
			].join('\n');

			const reviewTitle =
				preparation.mode === 'docs'
					? 'AI Dev Changed Documentation Review'
					: preparation.mode === 'code'
						? 'AI Dev Changed Code Review'
						: 'AI Dev Test Coverage Review';

			const reviewQuestion =
				target
					? includeAllMatches
						? `Review all ${preparation.mode} files matching ${target}`
						: `Review changed ${preparation.mode} files matching ${target}`
					: includeAllMatches
						? `Review all project ${preparation.mode}`
						: `Review changed project ${preparation.mode}`;

			const report = createAssistantReport({
				route: 'review',
				title: reviewTitle,
				question: reviewQuestion,
				modelName: this.modelName,
				warnings,
				rawResponse: combinedFindings,
			});

			this.reportSink?.(report);
			this.prepareForPermanentOutput();

			for (const warning of warnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			this.writePermanentLine(
				`${BULLET_CHAR} Files reviewed: ${preparation.changedFileCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Deterministic findings: ${preparation.deterministicFindingCount}`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				`${BULLET_CHAR} Review complete`,
				ANSI_LIGHT_GRAY
			);
			this.writePermanentLine(
				'  /showreport for findings',
				ANSI_LIGHT_GRAY
			);
		} catch (error) {
			this.prepareForPermanentOutput();

			if (cancellation.token.isCancellationRequested) {
				this.writePermanentLine(
					`${BULLET_CHAR} Cancelled`,
					ANSI_LIGHT_GRAY
				);
			} else {
				const message =
					error instanceof Error
						? error.message
						: String(error);

				this.writePermanentLine(
					`ERROR Review failed: ${message}`,
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

	private async submitAutomaticPrompt(
		question: string
	): Promise<void> {
		const route = chooseAutomaticAssistantRoute(question);

		if (route === 'summary' && this.summaryRoute) {
			await this.submitSummaryPrompt(question);
			return;
		}

		await this.submitChatPrompt(question);
	}

	private async submitSummaryPrompt(question: string): Promise<void> {
		const cancellation = new vscode.CancellationTokenSource();
		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.showEphemeralWithPrompt('Routing through summary documentation...');

		try {
			const routeResult = await this.summaryRoute!(question);

			if (cancellation.token.isCancellationRequested) {
				throw new Error('Assistant request cancelled.');
			}

			const responseText = await this.chatBackend.sendIsolatedMessage(
				routeResult.prompt,
				cancellation.token
			);

			this.prepareForPermanentOutput();

			const reportWarnings = [...routeResult.warnings];

			if (!responseText.trim()) {
				reportWarnings.push(
					'The model returned an empty response.'
				);
			}

			const report = createAssistantReport({
				route: 'summary',
				title: 'AI Dev Summary Answer',
				question,
				modelName: this.modelName,
				warnings: reportWarnings,
				rawResponse: responseText,
			});

			this.reportSink?.(report);

			for (const warning of reportWarnings) {
				this.writePermanentLine(
					`WARNING ${warning}`,
					ANSI_YELLOW
				);
			}

			if (reportWarnings.length > 0) {
				this.writePermanentLine(
					'  /showreport for details',
					ANSI_LIGHT_GRAY
				);
			}

			if (cancellation.token.isCancellationRequested) {
				this.writePermanentLine(
					`${BULLET_CHAR} Cancelled`,
					ANSI_LIGHT_GRAY
				);
			} else if (report.answer.trim()) {
				for (const line of formatModelResponseLines(
					this.modelName,
					report.answer
				)) {
					this.writePermanentLine(line, '');
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
					`ERROR Summary route failed: ${message}`,
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

	private async submitChatPrompt(prompt: string): Promise<void> {
		const cancellation = new vscode.CancellationTokenSource();
		this.requestCancellation = cancellation;
		this.requestInFlight = true;
		this.showEphemeralWithPrompt(`Thinking with ${this.modelName}...`);

		try {
			const responseText = await this.chatBackend.sendMessage(
				prompt,
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
					this.writePermanentLine(line, '');
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

	private async handleSubmittedCommand(command: string): Promise<boolean> {
		switch (parseSlashCommand(command).name) {
			case 'help':
				this.writePermanentLine(
					`${BULLET_CHAR} Available commands: /ask, /summarize, /review, /settings, /showreport, /exit`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Type / to discover commands`,
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
					`${BULLET_CHAR} Escape returns to chat`,
					ANSI_LIGHT_GRAY
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Up/Down navigate history`,
					ANSI_LIGHT_GRAY
				);
				return true;

			case 'settings': {
				const parsed = parseSlashCommand(command);

				if (
					parsed.options.includes('--help')
					|| parsed.options.includes('-h')
				) {
					const definition =
						getAssistantCommandDefinition('/settings');

					if (definition) {
						for (
							const line of
							formatAssistantCommandHelp(definition)
						) {
							this.writePermanentLine(
								line || ' ',
								ANSI_LIGHT_GRAY
							);
						}
					}

					return true;
				}

				const unknownOptions = parsed.options.filter(
					(option) => ![
						'--config',
						'-c',
					].includes(option)
				);

				if (unknownOptions.length > 0) {
					this.writePermanentLine(
						`WARNING Unknown /settings option: ${unknownOptions.join(', ')}`,
						ANSI_YELLOW
					);
					return true;
				}

				if (parsed.arguments.length > 0) {
					this.writePermanentLine(
						'WARNING Usage: /settings [--config] [--help]',
						ANSI_YELLOW
					);
					return true;
				}

				void vscode.commands.executeCommand(
					'aiDev.settings'
				);
				this.writePermanentLine(
					`${BULLET_CHAR} Opened AI Dev settings`,
					ANSI_LIGHT_GRAY
				);
				return true;
			}

			case 'ask': {
				const parsed = parseSlashCommand(command);

				if (
					parsed.options.includes('--help')
					|| parsed.options.includes('-h')
				) {
					const definition = getAssistantCommandDefinition('/ask');

					if (definition) {
						for (const line of formatAssistantCommandHelp(definition)) {
							this.writePermanentLine(
								line || ' ',
								ANSI_LIGHT_GRAY
							);
						}
					}

					return true;
				}

				const resolved = resolveAskCommand(parsed);

				if (!resolved.ok) {
					this.writePermanentLine(
						`WARNING ${resolved.error}`,
						ANSI_YELLOW
					);
					return true;
				}

				if (resolved.route === 'knowledgebase') {
					this.writePermanentLine(
						'WARNING The knowledgebase route is not connected yet.',
						ANSI_YELLOW
					);
					return true;
				}

				if (resolved.route === 'summary') {
					if (!this.summaryRoute) {
						this.writePermanentLine(
							'WARNING The summary route is not connected yet.',
							ANSI_YELLOW
						);
						return true;
					}

					await this.submitSummaryPrompt(resolved.question);
					return false;
				}

				if (resolved.route === 'auto') {
					await this.submitAutomaticPrompt(resolved.question);
					return false;
				}

				await this.submitChatPrompt(resolved.question);
				return false;
			}

			case 'summarize': {
				const parsed = parseSlashCommand(command);

				if (
					parsed.options.includes('--help')
					|| parsed.options.includes('-h')
				) {
					const definition =
						getAssistantCommandDefinition('/summarize');

					if (definition) {
						for (
							const line of
							formatAssistantCommandHelp(definition)
						) {
							this.writePermanentLine(
								line || ' ',
								ANSI_LIGHT_GRAY
							);
						}
					}

					return true;
				}

				if (
					parsed.options.includes('--config')
					|| parsed.options.includes('-c')
				) {
					if (!this.summarizationConfigOpener) {
						this.writePermanentLine(
							'WARNING Summarization configuration is unavailable.',
							ANSI_YELLOW
						);
						return true;
					}

					await this.summarizationConfigOpener();
					this.writePermanentLine(
						`${BULLET_CHAR} Opened summarization configuration`,
						ANSI_LIGHT_GRAY
					);
					return true;
				}

				const unknownOptions = parsed.options.filter(
					(option) => ![
						'--help',
						'-h',
						'--config',
						'-c',
						'--smoketest',
						'-s',
					].includes(option)
				);

				if (unknownOptions.length > 0) {
					this.writePermanentLine(
						`WARNING Unknown /summarize option: ${unknownOptions.join(', ')}`,
						ANSI_YELLOW
					);
					return true;
				}

				const target = parsed.arguments.join(' ').trim();

				if (!target) {
					this.writePermanentLine(
						'WARNING Usage: /summarize <file-path>',
						ANSI_YELLOW
					);
					return true;
				}

				const smokeTestRequested =
					parsed.options.includes('--smoketest')
					|| parsed.options.includes('-s');

				if (smokeTestRequested) {
					if (!this.summarizeRoute) {
						this.writePermanentLine(
							'WARNING The summarize route is not connected yet.',
							ANSI_YELLOW
						);
						return true;
					}

					await this.runSummarizeSmokeTest(target);
					return true;
				}

				if (!this.summarizeRoute) {
					this.writePermanentLine(
						'WARNING The summarize route is not connected yet.',
						ANSI_YELLOW
					);
					return true;
				}

				const isGlobTarget =
					/[*?\[\]{}]/.test(target);

				if (isGlobTarget) {
					await this.submitBatchSummarize(target);
				} else {
					await this.submitSummarizePrompt(target);
				}

				return false;
			}

			case 'review': {
				const parsed = parseSlashCommand(command);

				if (
					parsed.options.includes('--help')
					|| parsed.options.includes('-h')
				) {
					const definition =
						getAssistantCommandDefinition('/review');

					if (definition) {
						for (
							const line of
							formatAssistantCommandHelp(definition)
						) {
							this.writePermanentLine(
								line || ' ',
								ANSI_LIGHT_GRAY
							);
						}
					}

					return true;
				}

				const requestResolution =
					resolveReviewRequest(
						parsed.options,
						parsed.arguments
					);

				if (!requestResolution.ok) {
					this.writePermanentLine(
						`WARNING ${requestResolution.error}`,
						ANSI_YELLOW
					);
					return true;
				}

				if (!this.reviewRoute) {
					this.writePermanentLine(
						'WARNING The review route is not connected yet.',
						ANSI_YELLOW
					);
					return true;
				}

				if (requestResolution.request.smokeTest) {
					await this.runReviewSmokeTest(
						requestResolution.request
					);
					return true;
				}

				await this.submitReview(
					requestResolution.request
				);
				return false;
			}

			case 'showreport':
				if (!this.reportOpener?.()) {
					this.writePermanentLine(
						'WARNING No report is available for this session yet.',
						ANSI_YELLOW
					);
				}
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

	private async handleTab(): Promise<void> {
		if (this.state.mode !== 'command') {
			return;
		}

		this.clearTabCompletionLines();

		const pathContext =
			getPathCompletionContext(this.state.input);

		if (pathContext && this.pathCompleter) {
			await this.handlePathTab(pathContext);
			return;
		}

		const matches = getAssistantLookupItems(this.state.input);
		const prefixMatches = matches.filter(
			(match) => match.matchKind === 'prefix'
		);

		const completionMatch =
			prefixMatches.length === 1
				? prefixMatches[0]
				: matches.length === 1
					? matches[0]
					: undefined;

		if (completionMatch) {
			this.state = {
				...this.state,
				input: completionMatch.value,
				tabPressCount: 0,
			};
			this.refreshInteractiveArea();
			return;
		}

		if (this.state.tabPressCount !== 1) {
			this.state = {
				...this.state,
				tabPressCount: 1,
			};
			this.refreshInteractiveArea();
			return;
		}

		this.state = {
			...this.state,
			tabPressCount: 0,
		};

		this.clearInteractiveArea();
		this.drawPromptArea();

		const completionLines = matches.length === 0
			? ['No matching commands or options.']
			: matches.map((match) => match.display);

		this.showTabCompletionLines(completionLines);
	}

	private async handlePathTab(
		context: PathCompletionContext
	): Promise<void> {
		const result = await this.pathCompleter!(
			context.partialPath
		);
		const matches = result.matches;

		if (matches.length === 1) {
			const completed = matches[0];
			const closingQuote =
				context.quote && !completed.endsWith('/')
					? context.quote
					: '';

			this.state = {
				...this.state,
				input:
					`${context.beforePath}${context.quote}`
					+ `${completed}${closingQuote}`,
				tabPressCount: 0,
			};
			this.refreshInteractiveArea();
			return;
		}

		const commonPrefix = getCommonPrefix(matches);

		if (
			commonPrefix
			&& commonPrefix.length > context.partialPath.length
		) {
			this.state = {
				...this.state,
				input:
					`${context.beforePath}${context.quote}`
					+ commonPrefix,
				tabPressCount: 0,
			};
			this.refreshInteractiveArea();
			return;
		}

		if (this.state.tabPressCount !== 1) {
			this.state = {
				...this.state,
				tabPressCount: 1,
			};
			this.refreshInteractiveArea();
			return;
		}

		this.state = {
			...this.state,
			tabPressCount: 0,
		};

		this.clearInteractiveArea();
		this.drawPromptArea();

		const completionLines = matches.length === 0
			? ['No matching paths.']
			: formatItemsInColumns(matches, this.width);

		this.showTabCompletionLines(completionLines);
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
		const matches = getAssistantLookupItems(this.state.input);
		this.commandLookupLineCount = matches.length;

		if (matches.length === 0) {
			return;
		}

		this.writeRaw('\x1b7');
		this.writeRaw('\x1b[2B\r');

		for (const match of matches) {
			this.writeRaw(
				`${this.formatLookupItem(match)}\r\n`
			);
		}

		this.writeRaw('\x1b8');
	}

	private formatLookupItem(
		item: AssistantLookupItem
	): string {
		if (item.kind === 'command') {
			return formatCommandLookupLine(
				item.display,
				this.state.input
			);
		}

		return formatOptionLookupLine(
			item.display,
			this.state.input
		);
	}

	private showTabCompletionLines(
		lines: string[]
	): void {
		this.tabCompletionLineCount = lines.length;

		if (lines.length === 0 || !this.promptVisible) {
			return;
		}

		// Preserve the editable cursor, move beneath the prompt frame,
		// render ephemeral completion results, then restore the cursor.
		this.writeRaw('\x1b7');
		this.writeRaw('\x1b[2B\r');

		for (const line of lines) {
			this.writeRaw(
				`${ANSI_COMPLETION}${line}${ANSI_RESET}\r\n`
			);
		}

		this.writeRaw('\x1b8');
	}

	private clearTabCompletionLines(): void {
		if (
			this.tabCompletionLineCount === 0
			|| !this.promptVisible
		) {
			this.tabCompletionLineCount = 0;
			return;
		}

		this.writeRaw('\x1b7');
		this.writeRaw('\x1b[2B\r');
		this.writeRaw(
			`\x1b[${this.tabCompletionLineCount}M`
		);
		this.writeRaw('\x1b8');

		this.tabCompletionLineCount = 0;
	}

	private clearInteractiveArea(): void {
		this.clearTabCompletionLines();

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
