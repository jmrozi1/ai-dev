import * as vscode from 'vscode';

export interface OpenSettingsWebviewOptions {
	initialPromptOnly: boolean;
	initialDocsDir: string;
	initialBatchInitialSourceGlob: string;
	initialSourceExcludeGlobs: string[];
	onSave: (settings: {
		promptOnly?: boolean;
		docsDir?: string;
		batchInitialSourceGlob?: string;
		sourceExcludeGlobs?: string[];
	}) => Promise<void>;
}

function escapeHtmlAttribute(value: string): string {
	return value
		.replace(/&/g, '&amp;')
		.replace(/"/g, '&quot;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;');
}

function getWebviewHtml(
	initialPromptOnly: boolean,
	initialDocsDir: string,
	initialBatchInitialSourceGlob: string,
	initialSourceExcludeGlobs: string[]
): string {
	const checkedAttribute = initialPromptOnly ? ' checked' : '';
	const docsDirValue = escapeHtmlAttribute(initialDocsDir);
	const batchInitialSourceGlobValue = escapeHtmlAttribute(initialBatchInitialSourceGlob);
	const ignoredPathsValue = escapeHtmlAttribute(initialSourceExcludeGlobs.join('\n'));

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<title>AI Dev Settings</title>
	<style>
		:root {
			color-scheme: light dark;
		}

		body {
			font-family: var(--vscode-font-family);
			font-size: var(--vscode-font-size);
			color: var(--vscode-foreground);
			background: var(--vscode-editor-background);
			margin: 0;
			padding: 20px;
		}

		main {
			max-width: 720px;
		}

		h1 {
			font-size: 1.35rem;
			margin: 0 0 16px 0;
		}

		.setting-card {
			border: 1px solid var(--vscode-editorWidget-border, var(--vscode-panel-border));
			background: var(--vscode-editorWidget-background, var(--vscode-sideBar-background));
			border-radius: 8px;
			padding: 16px;
			display: grid;
			gap: 16px;
		}

		.label-row-inline {
			display: flex;
			align-items: center;
			gap: 10px;
			font-weight: 600;
		}

		.label-row {
			display: block;
			font-weight: 600;
			margin-bottom: 6px;
		}

		input[type='text'],
		textarea {
			width: 100%;
			box-sizing: border-box;
			padding: 8px 10px;
			border: 1px solid var(--vscode-input-border, transparent);
			background: var(--vscode-input-background);
			color: var(--vscode-input-foreground);
			border-radius: 4px;
			font-family: var(--vscode-font-family);
			font-size: var(--vscode-font-size);
		}

		textarea {
			min-height: 130px;
			resize: vertical;
		}

		.setting-description {
			margin: 4px 0 8px 0;
			color: var(--vscode-descriptionForeground);
			line-height: 1.4;
		}

		.label-row-inline + .setting-description {
			margin-left: 28px;
		}

		.actions {
			margin-top: 4px;
		}

		#status {
			font-size: 0.9rem;
			min-height: 1.2rem;
		}

		#status.success {
			color: var(--vscode-testing-iconPassed);
		}

		#status.error {
			color: var(--vscode-errorForeground);
		}

		#status.info {
			color: var(--vscode-descriptionForeground);
		}
	</style>
</head>
<body>
	<main>
		<h1>AI Dev Settings</h1>
		<section class="setting-card">
			<div>
				<label class="label-row-inline" for="promptOnly">
					<input id="promptOnly" type="checkbox"${checkedAttribute} />
					<span>Prompt Only</span>
				</label>
				<div class="setting-description">
					Prepare prompts for manual use instead of calling the VS Code language model directly.
				</div>
			</div>

			<div>
				<label class="label-row" for="docsDir">Docs directory</label>
				<div class="setting-description">
					Where AI Dev writes and reads generated summary docs.
				</div>
				<input id="docsDir" type="text" value="${docsDirValue}" />
			</div>

			<div>
				<label class="label-row" for="batchInitialSourceGlob">Batch initial source glob</label>
				<div class="setting-description">
					Default source pattern used when opening Batch Summary Doc Generation.
				</div>
				<input id="batchInitialSourceGlob" type="text" value="${batchInitialSourceGlobValue}" />
			</div>

			<div>
				<label class="label-row" for="ignoredPaths">Ignored paths</label>
				<div class="setting-description">
					Files matching these globs are excluded from AI Dev source discovery. One glob per line.
				</div>
				<textarea id="ignoredPaths">${ignoredPathsValue}</textarea>
			</div>

			<div class="actions">
				<div id="status" class="info" aria-live="polite"></div>
			</div>
		</section>
	</main>

	<script>
		const vscode = acquireVsCodeApi();
		const promptOnlyInput = document.getElementById('promptOnly');
		const docsDirInput = document.getElementById('docsDir');
		const batchInitialSourceGlobInput = document.getElementById('batchInitialSourceGlob');
		const ignoredPathsInput = document.getElementById('ignoredPaths');
		const statusElement = document.getElementById('status');
		let saveInFlight = false;
		let statusClearTimer = undefined;
		let lastSavedDocsDir = docsDirInput.value.trim();
		let lastSavedBatchInitialSourceGlob = batchInitialSourceGlobInput.value.trim();
		let lastSavedIgnoredPaths = ignoredPathsInput.value;

		function setStatus(message, tone, autoClear) {
			if (statusClearTimer) {
				clearTimeout(statusClearTimer);
				statusClearTimer = undefined;
			}

			statusElement.textContent = message;
			statusElement.className = tone;

			if (autoClear) {
				statusClearTimer = setTimeout(() => {
					statusElement.textContent = '';
					statusElement.className = 'info';
					statusClearTimer = undefined;
				}, 2500);
			}
		}

		function setSaveInFlight(inFlight) {
			saveInFlight = inFlight;
			promptOnlyInput.disabled = inFlight;
			docsDirInput.disabled = inFlight;
			batchInitialSourceGlobInput.disabled = inFlight;
			ignoredPathsInput.disabled = inFlight;
		}

		function getExcludeGlobsFromTextarea(value) {
			return value
				.replace(/\\r\\n/g, '\\n')
				.replace(/\\r/g, '\\n')
				.split('\\n')
				.map((line) => line.trim())
				.filter((line) => line.length > 0);
		}

		function save(payload) {
			if (saveInFlight) {
				return;
			}

			setSaveInFlight(true);
			setStatus('Saving...', 'info', false);
			vscode.postMessage({
				type: 'save',
				...payload,
			});
		}

		promptOnlyInput.addEventListener('change', () => {
			save({
				setting: 'promptOnly',
				promptOnly: promptOnlyInput.checked,
			});
		});

		function saveDocsDirIfChanged() {
			const trimmedValue = docsDirInput.value.trim();
			if (trimmedValue === lastSavedDocsDir) {
				return;
			}

			docsDirInput.value = trimmedValue;
			save({
				setting: 'docsDir',
				docsDir: trimmedValue,
			});
		}

		docsDirInput.addEventListener('blur', () => {
			saveDocsDirIfChanged();
		});

		docsDirInput.addEventListener('keydown', (event) => {
			if (event.key !== 'Enter') {
				return;
			}

			event.preventDefault();
			saveDocsDirIfChanged();
		});

		function saveBatchInitialSourceGlobIfChanged() {
			const trimmedValue = batchInitialSourceGlobInput.value.trim();
			if (trimmedValue === lastSavedBatchInitialSourceGlob) {
				return;
			}

			batchInitialSourceGlobInput.value = trimmedValue;
			save({
				setting: 'batchInitialSourceGlob',
				batchInitialSourceGlob: trimmedValue,
			});
		}

		batchInitialSourceGlobInput.addEventListener('blur', () => {
			saveBatchInitialSourceGlobIfChanged();
		});

		batchInitialSourceGlobInput.addEventListener('keydown', (event) => {
			if (event.key !== 'Enter') {
				return;
			}

			event.preventDefault();
			saveBatchInitialSourceGlobIfChanged();
		});

		function saveIgnoredPathsIfChanged() {
			if (ignoredPathsInput.value === lastSavedIgnoredPaths) {
				return;
			}

			save({
				setting: 'sourceExcludeGlobs',
				sourceExcludeGlobs: getExcludeGlobsFromTextarea(ignoredPathsInput.value),
			});
		}

		ignoredPathsInput.addEventListener('blur', () => {
			saveIgnoredPathsIfChanged();
		});

		window.addEventListener('message', (event) => {
			const message = event.data;
			if (!message || typeof message.type !== 'string') {
				return;
			}

			if (message.type === 'saveSuccess') {
				setSaveInFlight(false);
				if (message.setting === 'docsDir') {
					lastSavedDocsDir = docsDirInput.value.trim();
				}

				if (message.setting === 'batchInitialSourceGlob') {
					lastSavedBatchInitialSourceGlob = batchInitialSourceGlobInput.value.trim();
				}

				if (message.setting === 'sourceExcludeGlobs') {
					lastSavedIgnoredPaths = ignoredPathsInput.value;
				}

				setStatus('Settings saved.', 'success', true);
				return;
			}

			if (message.type === 'saveError') {
				setSaveInFlight(false);
				setStatus(message.error || 'Failed to save settings.', 'error', false);
			}
		});

		setStatus('', 'info', false);
	</script>
</body>
</html>`;
}

export function openSettingsWebview(
	context: vscode.ExtensionContext,
	options: OpenSettingsWebviewOptions
): void {
	const panel = vscode.window.createWebviewPanel(
		'aiDev.settings',
		'AI Dev Settings',
		vscode.ViewColumn.One,
		{
			enableScripts: true,
			retainContextWhenHidden: true,
		}
	);

	panel.webview.html = getWebviewHtml(
		options.initialPromptOnly,
		options.initialDocsDir,
		options.initialBatchInitialSourceGlob,
		options.initialSourceExcludeGlobs
	);

	panel.webview.onDidReceiveMessage(async (message: {
		type?: string;
		setting?: string;
		promptOnly?: boolean;
		docsDir?: string;
		batchInitialSourceGlob?: string;
		sourceExcludeGlobs?: string[];
	}) => {
		if (message.type !== 'save') {
			return;
		}

		const settings: {
			promptOnly?: boolean;
			docsDir?: string;
			batchInitialSourceGlob?: string;
			sourceExcludeGlobs?: string[];
		} = {};

		if (typeof message.promptOnly === 'boolean') {
			settings.promptOnly = message.promptOnly;
		}

		if (typeof message.docsDir === 'string') {
			settings.docsDir = message.docsDir;
		}

		if (typeof message.batchInitialSourceGlob === 'string') {
			settings.batchInitialSourceGlob = message.batchInitialSourceGlob;
		}

		if (Array.isArray(message.sourceExcludeGlobs)) {
			settings.sourceExcludeGlobs = message.sourceExcludeGlobs;
		}

		try {
			await options.onSave(settings);
			await panel.webview.postMessage({ type: 'saveSuccess', setting: message.setting });
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			await panel.webview.postMessage({ type: 'saveError', error: errorMessage });
		}
	});

	context.subscriptions.push(panel);
}
