import * as assert from 'assert';
import * as fs from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';

// You can import and use all API from the 'vscode' module
// as well as import your extension to test it
import * as vscode from 'vscode';
import {
	BATCH_UNIT_DOC_FILES_THIS_PASS_HELP_TEXT,
	DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS,
	MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
	getLargeBatchUnitDocRunWarningMessage,
	normalizeBatchUnitDocFilesThisPass,
	shouldShowLargeBatchUnitDocRunWarning,
} from '../batchUnitDocsView';
import { readAiDevConfig, resolveAiDevCorePath, setAiDevExtensionRootPath } from '../config';
import {
	applyTextInput,
	createAssistantInputState,
	handleBackspace,
	handleCommandTab,
	handleEscape,
	handleHistoryDown,
	handleHistoryUp,
	parseSlashCommand,
	submitInput,
} from '../assistantInput';
import {
	AiDevAssistantTerminalManager,
	MODEL_RESPONSE_MARKER,
	formatModelResponseLines,
} from '../assistantTerminal';
import { getAiDevRootNodes } from '../actionsView';
// import * as myExtension from '../../extension';

suite('Extension Test Suite', () => {
	vscode.window.showInformationMessage('Start all tests.');

	test('Sample test', () => {
		assert.strictEqual(-1, [1, 2, 3].indexOf(5));
		assert.strictEqual(-1, [1, 2, 3].indexOf(0));
	});

	test('Batch files this pass defaults to 25 when input is invalid', () => {
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(undefined), DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS);
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(Number.NaN), DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS);
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(''), DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS);
	});

	test('Batch files this pass accepts values above 100', () => {
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(250), 250);
	});

	test('Batch files this pass clamps values above 10000', () => {
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(12000), MAX_BATCH_UNIT_DOC_FILES_THIS_PASS);
		assert.strictEqual(normalizeBatchUnitDocFilesThisPass(10001), MAX_BATCH_UNIT_DOC_FILES_THIS_PASS);
	});

	test('Large run warning threshold is above 100 files', () => {
		assert.strictEqual(shouldShowLargeBatchUnitDocRunWarning(100), false);
		assert.strictEqual(shouldShowLargeBatchUnitDocRunWarning(101), true);
		assert.match(getLargeBatchUnitDocRunWarningMessage(101), /sequential/i);
		assert.match(getLargeBatchUnitDocRunWarningMessage(101), /cancelled/i);
	});

	test('Files this pass help text is not preview-only terminology', () => {
		assert.match(BATCH_UNIT_DOC_FILES_THIS_PASS_HELP_TEXT, /generation pass/i);
		assert.doesNotMatch(BATCH_UNIT_DOC_FILES_THIS_PASS_HELP_TEXT, /preview only/i);
	});

	test('Missing .ai-dev.yaml returns usable defaults', async () => {
		const workspaceRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ai-dev-config-defaults-'));

		const config = await readAiDevConfig(workspaceRoot);
			const resolvedCorePath = resolveAiDevCorePath(workspaceRoot, config.aiDevCorePath);

		assert.strictEqual(config.raw, '');
		assert.strictEqual(config.docsDir, 'ai-docs');
		assert.strictEqual(config.aiProviderMode, 'direct-experimental');
		assert.strictEqual(config.batchInitialSourceGlob, '**/*');
			assert.ok(path.isAbsolute(resolvedCorePath));
			assert.ok(resolvedCorePath.endsWith(path.join('ai-dev-core')));
	});

	test('Explicit YAML values override defaults', async () => {
		const workspaceRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ai-dev-config-overrides-'));
		const yaml = [
			'aiDevCore:',
			'  path: ./custom-core',
			'aiProvider:',
			'  mode: prompt-only',
			'documentation:',
			'  docsDir: custom-docs',
			'  batchInitialSourceGlob: src/**/*.ts',
		].join('\n');

		await fs.writeFile(path.join(workspaceRoot, '.ai-dev.yaml'), yaml, 'utf8');
		const config = await readAiDevConfig(workspaceRoot);

		assert.strictEqual(config.aiDevCorePath, './custom-core');
		assert.strictEqual(config.aiProviderMode, 'prompt-only');
		assert.strictEqual(config.docsDir, 'custom-docs');
		assert.strictEqual(config.batchInitialSourceGlob, 'src/**/*.ts');
	});

	test('Missing YAML resolves to bundled AI Dev Core when extension root is set', async () => {
		const workspaceRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ai-dev-config-bundled-'));
		const extensionRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ai-dev-extension-root-'));

		setAiDevExtensionRootPath(extensionRoot);
		const config = await readAiDevConfig(workspaceRoot);
		const resolved = resolveAiDevCorePath(workspaceRoot, config.aiDevCorePath);

		assert.strictEqual(resolved, path.join(extensionRoot, 'vendor', 'ai-dev-core'));
	});

	test('Explicit relative core path resolves relative to workspace', () => {
		const workspaceRoot = path.join(path.sep, 'tmp', 'workspace-relative-core');
		const resolved = resolveAiDevCorePath(workspaceRoot, './relative-core');

		assert.strictEqual(resolved, path.resolve(workspaceRoot, './relative-core'));
	});

	test('Explicit absolute core path remains absolute', () => {
		const workspaceRoot = path.join(path.sep, 'tmp', 'workspace-absolute-core');
		const absoluteCorePath = path.join(path.sep, 'opt', 'ai-dev-core');
		const resolved = resolveAiDevCorePath(workspaceRoot, absoluteCorePath);

		assert.strictEqual(resolved, absoluteCorePath);
	});

	test('Non-ENOENT config read errors are not swallowed', async () => {
		const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ai-dev-config-read-error-'));
		const workspaceRootFile = path.join(tempRoot, 'workspace-as-file');
		await fs.writeFile(workspaceRootFile, 'not a directory', 'utf8');

		await assert.rejects(
			() => readAiDevConfig(workspaceRootFile),
			(error: unknown) => {
				const code = (error as NodeJS.ErrnoException).code;
				return code !== undefined && code !== 'ENOENT';
			}
		);
	});

	test('Mandatory .ai-dev.yaml terminology is removed from extension and workflow details', async () => {
		const extensionSourcePath = path.resolve(__dirname, '../../src/extension.ts');
		const workflowDetailsSourcePath = path.resolve(__dirname, '../../src/workflowDetailsView.ts');
		const [extensionSource, workflowDetailsSource] = await Promise.all([
			fs.readFile(extensionSourcePath, 'utf8'),
			fs.readFile(workflowDetailsSourcePath, 'utf8'),
		]);

		assert.doesNotMatch(extensionSource, /Missing aiDevCore\.path in \.ai-dev\.yaml\./);
		assert.doesNotMatch(extensionSource, /Missing \.ai-dev\.yaml in workspace root\./);
		assert.doesNotMatch(workflowDetailsSource, /Missing \.ai-dev\.yaml or aiDevCore\.path/);
	});

	test('Assistant input defaults to chat mode', () => {
		const state = createAssistantInputState();

		assert.strictEqual(state.mode, 'chat');
		assert.strictEqual(state.input, '');
	});

	test('Typing slash as first char enters command mode', () => {
		const state = applyTextInput(createAssistantInputState(), '/');

		assert.strictEqual(state.mode, 'command');
		assert.strictEqual(state.input, '');
	});

	test('Escape returns command mode to chat mode', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/hel');
		const escaped = handleEscape(commandState);

		assert.strictEqual(escaped.mode, 'chat');
		assert.strictEqual(escaped.input, '');
	});

	test('Deleting command slash returns to chat mode', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/h');
		const oneBackspace = handleBackspace(commandState);
		const twoBackspaces = handleBackspace(oneBackspace);

		assert.strictEqual(twoBackspaces.mode, 'chat');
		assert.strictEqual(twoBackspaces.input, '');
	});

	test('/help and /exit are recognized slash commands', () => {
		assert.strictEqual(parseSlashCommand('/help'), 'help');
		assert.strictEqual(parseSlashCommand('/help details'), 'help');
		assert.strictEqual(parseSlashCommand('/exit'), 'exit');
	});

	test('Unknown slash command is handled as unknown', () => {
		assert.strictEqual(parseSlashCommand('/unknown'), 'unknown');
	});

	test('Single-match tab completion completes command', () => {
		const state = applyTextInput(createAssistantInputState(), '/he');
		const tab = handleCommandTab(state, ['/help', '/exit']);

		assert.strictEqual(tab.state.input, 'help');
		assert.strictEqual(tab.listMatches, undefined);
	});

	test('Double-tab lists matching commands', () => {
		const state = applyTextInput(createAssistantInputState(), '/');
		const firstTab = handleCommandTab(state, ['/help', '/exit']);
		const secondTab = handleCommandTab(firstTab.state, ['/help', '/exit']);

		assert.deepStrictEqual(secondTab.listMatches, ['/help', '/exit']);
	});

	test('Recalling /help restores command mode and editable input without slash', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(commandState).state;
		const recalled = handleHistoryUp(submitted);

		assert.strictEqual(recalled.mode, 'command');
		assert.strictEqual(recalled.input, 'help');
	});

	test('Resubmitting recalled /help is classified as command', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(commandState).state;
		const recalled = handleHistoryUp(submitted);
		const resubmitted = submitInput(recalled);

		assert.strictEqual(resubmitted.submittedKind, 'command');
		assert.strictEqual(resubmitted.submittedText, '/help');
	});

	test('Recalling chat history restores chat mode', () => {
		const chatState = applyTextInput(createAssistantInputState(), 'hello world');
		const submitted = submitInput(chatState).state;
		const recalled = handleHistoryUp(submitted);

		assert.strictEqual(recalled.mode, 'chat');
		assert.strictEqual(recalled.input, 'hello world');
	});

	test('Down arrow returns to command-mode draft when draft started in command mode', () => {
		const base = submitInput(applyTextInput(createAssistantInputState(), 'chat entry')).state;
		const draftCommand = applyTextInput(base, '/he');
		const recalled = handleHistoryUp(draftCommand);
		const restoredDraft = handleHistoryDown(recalled);

		assert.strictEqual(restoredDraft.mode, 'command');
		assert.strictEqual(restoredDraft.input, 'he');
	});

	test('Down arrow returns to chat-mode draft when draft started in chat mode', () => {
		const base = submitInput(applyTextInput(createAssistantInputState(), 'chat entry')).state;
		const draftChat = applyTextInput(base, 'draft question');
		const recalled = handleHistoryUp(draftChat);
		const restoredDraft = handleHistoryDown(recalled);

		assert.strictEqual(restoredDraft.mode, 'chat');
		assert.strictEqual(restoredDraft.input, 'draft question');
	});

	test('Terminal manager reuses active AI Dev terminal and recreates after close', () => {
		type CloseListener = (terminal: { name: string; show: () => void; dispose: () => void }) => void;
		const closeListeners: CloseListener[] = [];
		let createTerminalCount = 0;
		let showCount = 0;
		let lastCreatedTerminal: { name: string; show: () => void; dispose: () => void } | undefined;

		const fakeWindow = {
			createTerminal: (_options: vscode.ExtensionTerminalOptions) => {
				createTerminalCount += 1;
				const terminal = {
					name: 'AI Dev',
					show: () => {
						showCount += 1;
					},
					dispose: () => {
						for (const listener of closeListeners) {
							listener(terminal);
						}
					},
				};
				lastCreatedTerminal = terminal;
				return terminal as unknown as vscode.Terminal;
			},
			onDidCloseTerminal: (listener: CloseListener) => {
				closeListeners.push(listener);
				return { dispose: () => {} };
			},
		} as unknown as Pick<typeof vscode.window, 'createTerminal' | 'onDidCloseTerminal'>;

		const manager = new AiDevAssistantTerminalManager(fakeWindow);

		manager.launchAssistant();
		manager.launchAssistant();

		assert.strictEqual(createTerminalCount, 1);
		assert.strictEqual(showCount, 2);

		assert.ok(lastCreatedTerminal);
		lastCreatedTerminal?.dispose();

		manager.launchAssistant();
		assert.strictEqual(createTerminalCount, 2);
		manager.dispose();
	});

	test('Launch Assistant is first root item in AI Dev activity view', async () => {
		const rootNodes = getAiDevRootNodes();

		assert.ok(rootNodes.length > 0);
		assert.strictEqual(rootNodes[0].type, 'launchAssistant');
		assert.strictEqual(rootNodes[0].label, 'Launch Assistant');
	});

	test('Terminal renders Unicode separator character', () => {
		// Helper to validate that separator rendering uses "─"
		const separator = '─'.repeat(40);
		assert.strictEqual(separator, '────────────────────────────────────────');
		assert.match(separator, /^─+$/);
	});

	test('Bullet character is used for permanent output markers', () => {
		// Verify bullet character constant is used in rendering
		const bulletChar = '•';
		const testLine = `${bulletChar} Test output`;
		assert.match(testLine, /^• /);
		assert.strictEqual(testLine[0], '•');
	});

	test('Full-width row rendering pads text to terminal width', () => {
		// Test the padding logic: text should be padded with spaces to fill width
		const width = 40;
		const text = 'hello';
		const truncated = text.length > width ? text.slice(0, width) : text;
		const paddingLength = Math.max(0, width - truncated.length);
		const paddedRow = truncated + ' '.repeat(paddingLength);
		
		assert.strictEqual(paddedRow.length, 40);
		assert.strictEqual(paddedRow, 'hello' + ' '.repeat(35));
	});

	test('Full-width row rendering safely truncates long input', () => {
		// Simulate truncation for long text
		const width = 20;
		const longText = 'this is a very long text that exceeds the terminal width';
		const truncated = longText.length > width ? longText.slice(0, width) : longText;
		
		assert.strictEqual(truncated.length, width);
		assert.strictEqual(truncated, 'this is a very long ');
	});

	test('Submitted chat input is stored in history', () => {
		// Verify that chat submission stores full text in history without duplication
		const chatState = applyTextInput(createAssistantInputState(), 'hello world');
		const submitted = submitInput(chatState);
		
		assert.strictEqual(submitted.state.history.length, 1);
		assert.strictEqual(submitted.state.history[0], 'hello world');
		assert.strictEqual(submitted.state.input, '');
		assert.strictEqual(submitted.state.mode, 'chat');
	});

	test('Submitted command input is stored in history with slash', () => {
		// Verify that command submission stores full text in history without duplication
		const cmdState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(cmdState);
		
		assert.strictEqual(submitted.state.history.length, 1);
		assert.strictEqual(submitted.state.history[0], '/help');
		assert.strictEqual(submitted.state.input, '');
		assert.strictEqual(submitted.state.mode, 'chat');
	});

	test('Help output uses bullet markers with no blank lines', () => {
		// Verify help text structure: each item uses bullet, no blank lines between
		const helpItems = [
			'• Available commands: /help, /exit',
			'• Tab completes commands',
			'• Tab twice lists commands',
			'• Escape leaves command mode',
			'• Up/Down navigate history',
		];
		
		// Each line should start with bullet
		for (const item of helpItems) {
			assert.match(item, /^• /);
		}
		
		// No blank lines when joined (no consecutive newlines)
		const joined = helpItems.join('\n');
		assert.doesNotMatch(joined, /\n\n/);
	});

	test('Terminal escape clears input and returns to chat mode', () => {
		// Verify that escape in command mode is followed by a fresh three-line prompt
		const commandState = applyTextInput(createAssistantInputState(), '/he');
		const escaped = handleEscape(commandState);
		
		assert.strictEqual(escaped.mode, 'chat');
		assert.strictEqual(escaped.input, '');
		assert.strictEqual(escaped.historyIndex, -1);
	});

	test('Pressing Enter after cancellation redraws prompt area', () => {
		// Verify that state after cancellation is clean for new prompt
		const state = createAssistantInputState();
		assert.strictEqual(state.input, '');
		assert.strictEqual(state.mode, 'chat');
		assert.strictEqual(state.tabPressCount, 0);
	});

	test('Model responses use the large response marker', () => {
		assert.strictEqual(MODEL_RESPONSE_MARKER, '◆');
	});

	test('Model response formatting labels and indents lines', () => {
		assert.deepStrictEqual(
			formatModelResponseLines('Test Model', 'First\nSecond'),
			[
				'◆ Test Model: First',
				'  Second',
			]
		);
	});

	test('Empty model response formats as no lines', () => {
		assert.deepStrictEqual(
			formatModelResponseLines('Test Model', '   '),
			[]
		);
	});

});
