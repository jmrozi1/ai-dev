import * as path from 'node:path';
import * as vscode from 'vscode';

export type BatchUnitDocStatus = 'missing' | 'empty' | 'exists';
export type BatchUnitDocActionType = 'generate-doc' | 'move-doc' | 'delete-doc';

export interface BatchUnitDocPreviewItem {
	apply: boolean;
	sourcePath?: string;
	docPath: string;
	actionType: BatchUnitDocActionType;
	actionLabel: 'Update summary' | 'Move orphan doc' | 'Delete orphan doc';
	notes: string;
	targetDocPath?: string;
}

export interface BatchUnitDocPreviewCounts {
	totalConfiguredSourceCandidates: number;
	afterGlobFilter: number;
	afterMissingDocFilter: number;
	previewCount: number;
}

export interface BatchUnitDocPatternWarning {
	message: string;
	configuredPattern: string;
	recommendedPattern: string;
}

export interface BatchUnitDocPreviewResult {
	counts: BatchUnitDocPreviewCounts;
	items: BatchUnitDocPreviewItem[];
	flatteningPatternWarning?: BatchUnitDocPatternWarning;
}

export interface BatchUnitDocsFormState {
	sourceGlob: string;
	missingDocsOnly: boolean;
	resolveOrphanedDocs: boolean;
	maxFiles: number;
	selectionMode: 'workspace' | 'folder';
	selectedSourceDirectory?: string;
	selectedSummaryFile?: string;
}

interface OpenBatchUnitDocsWebviewOptions {
	workspaceRoot: string;
	initialState: BatchUnitDocsFormState;
	onPreview: (state: BatchUnitDocsFormState) => Promise<BatchUnitDocPreviewResult>;
	onGenerateRequested?: (state: BatchUnitDocsFormState, previewPlan: BatchUnitDocPreviewResult) => Promise<void>;
}

function serializeFormState(state: BatchUnitDocsFormState): string {
	return JSON.stringify({
		sourceGlob: state.sourceGlob,
		missingDocsOnly: state.missingDocsOnly,
		resolveOrphanedDocs: state.resolveOrphanedDocs,
		maxFiles: state.maxFiles,
		selectionMode: state.selectionMode,
		selectedSourceDirectory: state.selectedSourceDirectory,
		selectedSummaryFile: state.selectedSummaryFile,
	});
}

function getPathLabel(workspaceRoot: string, state: BatchUnitDocsFormState): string {
	if (state.selectionMode !== 'folder') {
		return workspaceRoot;
	}

	const normalizedDirectory = state.selectedSourceDirectory?.trim();
	if (!normalizedDirectory || normalizedDirectory === '.') {
		return workspaceRoot;
	}

	return path.resolve(workspaceRoot, normalizedDirectory);
}

function createNonce(): string {
	const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
	let nonce = '';
	for (let index = 0; index < 32; index += 1) {
		nonce += chars.charAt(Math.floor(Math.random() * chars.length));
	}
	return nonce;
}

function escapeHtml(text: string): string {
	return text
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}

function getWebviewHtml(webview: vscode.Webview, options: OpenBatchUnitDocsWebviewOptions): string {
	const nonce = createNonce();
	const initialStateJson = JSON.stringify(options.initialState);
	const pathLabel = escapeHtml(getPathLabel(options.workspaceRoot, options.initialState));

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';" />
	<title>Batch Summary Generation</title>
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
			padding: 16px;
		}
		h1 {
			margin: 0 0 12px;
			font-size: 1.25rem;
		}
		p {
			margin: 6px 0;
		}
		.form-grid {
			display: grid;
			grid-template-columns: 1fr;
			gap: 12px;
			margin-bottom: 12px;
		}
		.field {
			display: flex;
			flex-direction: column;
			gap: 6px;
		}
		.field-inline {
			display: flex;
			align-items: center;
			gap: 8px;
		}
		input[type="text"],
		input[type="number"] {
			padding: 6px 8px;
			border: 1px solid var(--vscode-editorWidget-border);
			background: var(--vscode-input-background);
			color: var(--vscode-input-foreground);
		}
		.help {
			color: var(--vscode-descriptionForeground);
			font-size: 0.92em;
		}
		.actions {
			display: flex;
			gap: 8px;
			margin: 10px 0 14px;
		}
		button {
			padding: 7px 12px;
			border: 1px solid var(--vscode-button-border, transparent);
			background: var(--vscode-button-background);
			color: var(--vscode-button-foreground);
			cursor: pointer;
		}
		button.secondary {
			background: var(--vscode-button-secondaryBackground);
			color: var(--vscode-button-secondaryForeground);
		}
		button:disabled {
			opacity: 0.55;
			cursor: not-allowed;
		}
		.status {
			min-height: 20px;
			margin: 4px 0 10px;
		}
		.status.error {
			color: var(--vscode-errorForeground);
		}
		.status.info {
			color: var(--vscode-descriptionForeground);
		}
		table {
			width: 100%;
			border-collapse: collapse;
		}
		th,
		td {
			text-align: left;
			vertical-align: top;
			padding: 6px 8px;
			border-bottom: 1px solid var(--vscode-editorWidget-border);
			word-break: break-word;
		}
		th {
			font-weight: 600;
		}
		.chip {
			display: inline-block;
			padding: 2px 8px;
			border-radius: 999px;
			font-size: 0.85em;
		}
		.chip.missing {
			background: color-mix(in srgb, var(--vscode-testing-iconPassed) 20%, transparent);
			color: var(--vscode-testing-iconPassed);
		}
		.chip.move {
			background: color-mix(in srgb, var(--vscode-testing-iconQueued) 22%, transparent);
			color: var(--vscode-testing-iconQueued);
		}
		.chip.delete {
			background: color-mix(in srgb, var(--vscode-testing-iconFailed) 20%, transparent);
			color: var(--vscode-testing-iconFailed);
		}
		.summary {
			margin: 8px 0 10px;
		}
		.pattern-warning {
			display: none;
			margin: 8px 0 10px;
			padding: 8px 10px;
			border-left: 3px solid var(--vscode-editorWarning-foreground);
			background: color-mix(in srgb, var(--vscode-editorWarning-foreground) 12%, transparent);
		}
		.pattern-warning.visible {
			display: block;
		}
		@media (max-width: 780px) {
			th,
			td {
				font-size: 0.92em;
			}
		}
	</style>
</head>
<body>
	<h1>Batch Summary Doc Generation</h1>
	<p><strong>Path:</strong> <span id="pathLabel">${pathLabel}</span></p>
	<div class="form-grid">
		<label class="field">
			<span>Source glob input</span>
			<input id="sourceGlob" type="text" value="" />
			<span class="help">Additional filter applied after .ai-dev.yaml source.exclude.</span>
		</label>

		<label class="field-inline">
			<input id="missingDocsOnly" type="checkbox" />
			<span>Missing or empty summaries only</span>
		</label>
		<p class="help">When checked, include only source files whose summary file is missing or empty. When unchecked, include matching source files even if summary content already exists.</p>

		<label class="field-inline">
			<input id="resolveOrphanedDocs" type="checkbox" />
			<span>Resolve orphaned docs</span>
		</label>
		<p class="help">Deletes non-summary docs that do not map to the source-to-summary path model. Summary files are never moved or deleted.</p>

		<label class="field">
			<span>Max files input</span>
			<input id="maxFiles" type="number" min="1" max="100" value="25" />
			<span class="help">Preview only limit in this pass (1-100).</span>
		</label>
	</div>

	<div class="actions">
		<button id="previewButton" type="button">Preview</button>
		<button id="generateButton" type="button" class="secondary">Generate Summary Docs</button>
	</div>

	<div id="status" class="status info"></div>
	<div id="patternWarning" class="pattern-warning"></div>
	<div id="summary" class="summary"></div>
	<table>
		<thead>
			<tr>
				<th>Apply</th>
				<th>Source Path</th>
				<th>Summary File</th>
				<th>Action</th>
				<th>Notes</th>
			</tr>
		</thead>
		<tbody id="resultsBody">
			<tr><td colspan="5">No preview yet.</td></tr>
		</tbody>
	</table>

	<script nonce="${nonce}">
		const vscode = acquireVsCodeApi();
		const initialState = ${initialStateJson};

		const sourceGlobInput = document.getElementById('sourceGlob');
		const missingDocsOnlyInput = document.getElementById('missingDocsOnly');
		const resolveOrphanedDocsInput = document.getElementById('resolveOrphanedDocs');
		const maxFilesInput = document.getElementById('maxFiles');
		const previewButton = document.getElementById('previewButton');
		const generateButton = document.getElementById('generateButton');
		const statusEl = document.getElementById('status');
		const patternWarningEl = document.getElementById('patternWarning');
		const summaryEl = document.getElementById('summary');
		const resultsBody = document.getElementById('resultsBody');

		sourceGlobInput.value = initialState.sourceGlob;
		missingDocsOnlyInput.checked = !!initialState.missingDocsOnly;
		resolveOrphanedDocsInput.checked = !!initialState.resolveOrphanedDocs;
		maxFilesInput.value = String(initialState.maxFiles);

		const getFormState = () => {
			const rawMax = Number.parseInt(String(maxFilesInput.value ?? '').trim(), 10);
			const safeMax = Number.isFinite(rawMax) ? Math.min(100, Math.max(1, rawMax)) : 25;
			return {
				sourceGlob: String(sourceGlobInput.value ?? '').trim() || '**/*',
				missingDocsOnly: !!missingDocsOnlyInput.checked,
				resolveOrphanedDocs: !!resolveOrphanedDocsInput.checked,
				maxFiles: safeMax,
				selectionMode: initialState.selectionMode === 'folder' ? 'folder' : 'workspace',
				selectedSourceDirectory: initialState.selectedSourceDirectory,
				selectedSummaryFile: initialState.selectedSummaryFile,
			};
		};

		const hasAppliedRows = () => Array.isArray(lastPreviewPayload?.items)
			&& lastPreviewPayload.items.some((item) => item && item.apply);

		const setStatus = (text, kind) => {
			statusEl.textContent = text;
			statusEl.className = 'status ' + kind;
		};

		let previewInFlight = false;
		let generateInFlight = false;
		let previewReadyForCurrentState = false;
		let lastPreviewPayload = undefined;

		const setButtonsDisabled = () => {
			previewButton.disabled = previewInFlight || generateInFlight;
			generateButton.disabled = generateInFlight || !previewReadyForCurrentState || !hasAppliedRows();
		};

		const markPreviewStale = (showMessage) => {
			if (previewReadyForCurrentState) {
				previewReadyForCurrentState = false;
				lastPreviewPayload = undefined;
				setButtonsDisabled();
				if (showMessage) {
					setStatus('Form changed. Preview again before generating summaries.', 'info');
				}
			}
		};

		const renderPreview = (payload) => {
			const counts = payload.counts;
			const items = payload.items;
			const flatteningWarning = payload.flatteningPatternWarning;
			lastPreviewPayload = payload;
			summaryEl.textContent = [
				'Total configured source candidates: ' + counts.totalConfiguredSourceCandidates,
				'After glob filter: ' + counts.afterGlobFilter,
				'After summary-needed filter: ' + counts.afterMissingDocFilter,
				'Planned rows: ' + counts.previewCount,
			].join(' | ');

			if (flatteningWarning) {
				patternWarningEl.className = 'pattern-warning visible';
				patternWarningEl.textContent = [
					flatteningWarning.message,
					'Configured pattern: ' + flatteningWarning.configuredPattern,
					'Recommended pattern: ' + flatteningWarning.recommendedPattern,
				].join(' ');
			} else {
				patternWarningEl.className = 'pattern-warning';
				patternWarningEl.textContent = '';
			}

			resultsBody.innerHTML = '';
			if (!Array.isArray(items) || items.length === 0) {
				const row = document.createElement('tr');
				const cell = document.createElement('td');
				cell.colSpan = 5;
				cell.textContent = 'No planned actions for this configuration.';
				row.appendChild(cell);
				resultsBody.appendChild(row);
				return;
			}

			for (const [index, item] of items.entries()) {
				const row = document.createElement('tr');

				const applyCell = document.createElement('td');
				const applyCheckbox = document.createElement('input');
				applyCheckbox.type = 'checkbox';
				applyCheckbox.checked = !!item.apply;
				applyCheckbox.setAttribute('aria-label', 'Apply row ' + (index + 1));
				applyCheckbox.addEventListener('change', () => {
					item.apply = !!applyCheckbox.checked;
					setButtonsDisabled();
				});
				applyCell.appendChild(applyCheckbox);
				row.appendChild(applyCell);

				const sourceCell = document.createElement('td');
				sourceCell.textContent = item.sourcePath || '—';
				row.appendChild(sourceCell);

				const docCell = document.createElement('td');
				docCell.textContent = item.docPath;
				row.appendChild(docCell);

				const actionCell = document.createElement('td');
				const chip = document.createElement('span');
				const chipClass = item.actionType === 'generate-doc'
					? 'missing'
					: item.actionType === 'move-doc'
						? 'move'
						: 'delete';
				chip.className = 'chip ' + chipClass;
				chip.textContent = item.actionLabel;
				actionCell.appendChild(chip);
				row.appendChild(actionCell);

				const notesCell = document.createElement('td');
				notesCell.textContent = item.notes;
				row.appendChild(notesCell);

				resultsBody.appendChild(row);
			}
			setButtonsDisabled();
		};

		previewButton.addEventListener('click', () => {
			const state = getFormState();
			previewInFlight = true;
			setButtonsDisabled();
			setStatus('Computing preview...', 'info');
			vscode.postMessage({ type: 'previewCandidates', state });
		});

		generateButton.addEventListener('click', () => {
			if (!previewReadyForCurrentState) {
				setStatus('Preview candidates first before generating summaries.', 'info');
				return;
			}

			if (!hasAppliedRows()) {
				setStatus('Select at least one row to generate summaries.', 'info');
				return;
			}

			const state = getFormState();
			generateInFlight = true;
			setButtonsDisabled();
			setStatus('Starting summary generation...', 'info');
			vscode.postMessage({ type: 'generateDocs', state, previewPayload: lastPreviewPayload });
		});

		sourceGlobInput.addEventListener('input', () => markPreviewStale(true));
		missingDocsOnlyInput.addEventListener('change', () => markPreviewStale(true));
		resolveOrphanedDocsInput.addEventListener('change', () => markPreviewStale(true));
		maxFilesInput.addEventListener('input', () => markPreviewStale(true));

		window.addEventListener('message', (event) => {
			const message = event.data;
			if (!message || typeof message.type !== 'string') {
				return;
			}

			if (message.type === 'previewResult') {
				previewInFlight = false;
				previewReadyForCurrentState = true;
				setButtonsDisabled();
				setStatus('Preview updated.', 'info');
				renderPreview(message.payload);
				return;
			}

			if (message.type === 'previewError') {
				previewInFlight = false;
				previewReadyForCurrentState = false;
				lastPreviewPayload = undefined;
				setButtonsDisabled();
				setStatus(message.error || 'Failed to compute preview.', 'error');
				return;
			}

			if (message.type === 'generateError') {
				generateInFlight = false;
				setButtonsDisabled();
				setStatus(message.error || 'Failed to start generation.', 'error');
				return;
			}

			if (message.type === 'generateStarted') {
				generateInFlight = false;
				setButtonsDisabled();
				setStatus('Batch generation started. Progress is shown in notifications.', 'info');
			}
		});

		setStatus('Preview to enable generation.', 'info');
		setButtonsDisabled();
	</script>
</body>
</html>`;
}

export async function openBatchUnitDocsWebview(
	context: vscode.ExtensionContext,
	options: OpenBatchUnitDocsWebviewOptions
): Promise<void> {
	const panel = vscode.window.createWebviewPanel(
		'aiDev.batchUnitDocsPreview',
		'Batch Summary Generation',
		vscode.ViewColumn.One,
		{
			enableScripts: true,
			retainContextWhenHidden: true,
		}
	);

	panel.webview.html = getWebviewHtml(panel.webview, options);

	let lastSuccessfulPreviewStateKey: string | undefined;

	panel.webview.onDidReceiveMessage(async (message: { type?: string; state?: BatchUnitDocsFormState; previewPayload?: BatchUnitDocPreviewResult }) => {
		const incomingState = message.state;
		const normalizedState: BatchUnitDocsFormState | undefined = incomingState
			? {
				sourceGlob: incomingState.sourceGlob.trim().length > 0 ? incomingState.sourceGlob.trim() : '**/*',
				missingDocsOnly: Boolean(incomingState.missingDocsOnly),
				resolveOrphanedDocs: Boolean(incomingState.resolveOrphanedDocs),
				maxFiles: Number.isFinite(incomingState.maxFiles)
					? Math.min(100, Math.max(1, Math.floor(incomingState.maxFiles)))
					: 25,
				selectionMode: incomingState.selectionMode === 'folder' ? 'folder' : 'workspace',
				selectedSourceDirectory: incomingState.selectedSourceDirectory?.trim() || undefined,
				selectedSummaryFile: incomingState.selectedSummaryFile?.trim() || undefined,
			}
			: undefined;

		if (message.type === 'previewCandidates') {
			if (!normalizedState) {
				await panel.webview.postMessage({ type: 'previewError', error: 'Missing preview configuration.' });
				return;
			}

			try {
				const preview = await options.onPreview(normalizedState);
				lastSuccessfulPreviewStateKey = serializeFormState(normalizedState);
				await panel.webview.postMessage({ type: 'previewResult', payload: preview });
			} catch (error) {
				lastSuccessfulPreviewStateKey = undefined;
				const messageText = error instanceof Error ? error.message : String(error);
				await panel.webview.postMessage({ type: 'previewError', error: messageText });
			}
			return;
		}

		if (message.type === 'generateDocs') {
			if (!options.onGenerateRequested) {
				await panel.webview.postMessage({ type: 'generateError', error: 'Generate Summaries is not implemented yet.' });
				return;
			}

			if (!normalizedState) {
				await panel.webview.postMessage({ type: 'generateError', error: 'Missing generation configuration.' });
				return;
			}

			const normalizedStateKey = serializeFormState(normalizedState);
			if (!lastSuccessfulPreviewStateKey || lastSuccessfulPreviewStateKey !== normalizedStateKey) {
				await panel.webview.postMessage({
					type: 'generateError',
					error: 'Preview first, then generate without changing the form.',
				});
				return;
			}

			if (!message.previewPayload || !Array.isArray(message.previewPayload.items)) {
				await panel.webview.postMessage({ type: 'generateError', error: 'Missing preview plan. Run Preview again.' });
				return;
			}

			await panel.webview.postMessage({ type: 'generateStarted' });
			await options.onGenerateRequested(normalizedState, message.previewPayload);
		}
	});

	context.subscriptions.push(panel);
}
