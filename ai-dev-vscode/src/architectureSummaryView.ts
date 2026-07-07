import * as vscode from 'vscode';

export type ArchitectureSummaryStatus = 'missing' | 'empty' | 'exists';

export interface ArchitectureSummaryPreviewItem {
	apply: boolean;
	sourceDirectory: string;
	summaryPath: string;
	status: ArchitectureSummaryStatus;
	notes: string;
}

export interface ArchitectureSummaryPreviewCounts {
	totalDirectories: number;
	existingSummaries: number;
	missingSummaries: number;
	emptySummaries: number;
}

export interface ArchitectureSummaryPreviewResult {
	counts: ArchitectureSummaryPreviewCounts;
	items: ArchitectureSummaryPreviewItem[];
}

interface OpenArchitectureSummaryWebviewOptions {
	workspaceRoot: string;
	onPreview: () => Promise<ArchitectureSummaryPreviewResult>;
	onGenerateRequested?: (previewPlan: ArchitectureSummaryPreviewResult) => Promise<void>;
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

function getWebviewHtml(webview: vscode.Webview, options: OpenArchitectureSummaryWebviewOptions): string {
	const nonce = createNonce();
	const pathLabel = escapeHtml(options.workspaceRoot);

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';" />
	<title>Generate Architecture Summary</title>
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
		.actions {
			display: flex;
			gap: 8px;
			margin: 12px 0 14px;
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
		.chip.exists {
			background: color-mix(in srgb, var(--vscode-testing-iconPassed) 18%, transparent);
			color: var(--vscode-testing-iconPassed);
		}
		.chip.missing {
			background: color-mix(in srgb, var(--vscode-testing-iconFailed) 16%, transparent);
			color: var(--vscode-testing-iconFailed);
		}
		.chip.empty {
			background: color-mix(in srgb, var(--vscode-testing-iconQueued) 20%, transparent);
			color: var(--vscode-testing-iconQueued);
		}
		.summary {
			margin: 8px 0 10px;
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
	<h1>Generate Architecture Summary</h1>
	<p><strong>Path:</strong> <span id="pathLabel">${pathLabel}</span></p>

	<div class="actions">
		<button id="previewButton" type="button">Preview</button>
		<button id="generateButton" type="button" class="secondary">Generate Architecture Summary</button>
	</div>

	<div id="status" class="status info"></div>
	<div id="summary" class="summary"></div>
	<table>
		<thead>
			<tr>
				<th>Apply</th>
				<th>Directory</th>
				<th>Summary</th>
				<th>Status</th>
				<th>Notes</th>
			</tr>
		</thead>
		<tbody id="resultsBody">
			<tr><td colspan="5">No preview yet.</td></tr>
		</tbody>
	</table>

	<script nonce="${nonce}">
		const vscode = acquireVsCodeApi();
		const previewButton = document.getElementById('previewButton');
		const generateButton = document.getElementById('generateButton');
		const statusEl = document.getElementById('status');
		const summaryEl = document.getElementById('summary');
		const resultsBody = document.getElementById('resultsBody');

		const hasAppliedRows = () => Array.isArray(lastPreviewPayload?.items)
			&& lastPreviewPayload.items.some((item) => item && item.apply);

		const setStatus = (text, kind) => {
			statusEl.textContent = text;
			statusEl.className = 'status ' + kind;
		};

		let previewInFlight = false;
		let generateInFlight = false;
		let previewReady = false;
		let lastPreviewPayload = undefined;

		const setButtonsDisabled = () => {
			previewButton.disabled = previewInFlight || generateInFlight;
			generateButton.disabled = generateInFlight || !previewReady || !hasAppliedRows();
		};

		const renderPreview = (payload) => {
			const counts = payload.counts;
			const items = payload.items;
			lastPreviewPayload = payload;
			summaryEl.textContent = [
				'Total directories: ' + counts.totalDirectories,
				'Existing summaries: ' + counts.existingSummaries,
				'Missing summaries: ' + counts.missingSummaries,
				'Empty summaries: ' + counts.emptySummaries,
			].join(' | ');

			resultsBody.innerHTML = '';
			if (!Array.isArray(items) || items.length === 0) {
				const row = document.createElement('tr');
				const cell = document.createElement('td');
				cell.colSpan = 5;
				cell.textContent = 'No directories discovered for architecture summary generation.';
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

				const directoryCell = document.createElement('td');
				directoryCell.textContent = item.sourceDirectory;
				row.appendChild(directoryCell);

				const summaryPathCell = document.createElement('td');
				summaryPathCell.textContent = item.summaryPath;
				row.appendChild(summaryPathCell);

				const statusCell = document.createElement('td');
				const chip = document.createElement('span');
				chip.className = 'chip ' + item.status;
				chip.textContent = item.status;
				statusCell.appendChild(chip);
				row.appendChild(statusCell);

				const notesCell = document.createElement('td');
				notesCell.textContent = item.notes;
				row.appendChild(notesCell);

				resultsBody.appendChild(row);
			}

			setButtonsDisabled();
		};

		previewButton.addEventListener('click', () => {
			previewInFlight = true;
			previewReady = false;
			lastPreviewPayload = undefined;
			setButtonsDisabled();
			setStatus('Computing preview...', 'info');
			vscode.postMessage({ type: 'previewCandidates' });
		});

		generateButton.addEventListener('click', () => {
			if (!previewReady) {
				setStatus('Preview candidates first before generating architecture summary.', 'info');
				return;
			}

			if (!hasAppliedRows()) {
				setStatus('Select at least one row before generating architecture summary.', 'info');
				return;
			}

			generateInFlight = true;
			setButtonsDisabled();
			setStatus('Starting architecture summary generation...', 'info');
			vscode.postMessage({ type: 'generateArchitectureSummary', previewPayload: lastPreviewPayload });
		});

		window.addEventListener('message', (event) => {
			const message = event.data;
			if (!message || typeof message.type !== 'string') {
				return;
			}

			if (message.type === 'previewResult') {
				previewInFlight = false;
				previewReady = true;
				setButtonsDisabled();
				setStatus('Preview updated.', 'info');
				renderPreview(message.payload);
				return;
			}

			if (message.type === 'previewError') {
				previewInFlight = false;
				previewReady = false;
				lastPreviewPayload = undefined;
				setButtonsDisabled();
				setStatus(message.error || 'Failed to compute preview.', 'error');
				return;
			}

			if (message.type === 'generateError') {
				generateInFlight = false;
				setButtonsDisabled();
				setStatus(message.error || 'Failed to start architecture summary generation.', 'error');
				return;
			}

			if (message.type === 'generateStarted') {
				generateInFlight = false;
				setButtonsDisabled();
				setStatus('Architecture summary generation started. Progress is shown in notifications.', 'info');
			}
		});

		setStatus('Preview to enable architecture summary generation.', 'info');
		setButtonsDisabled();
	</script>
</body>
</html>`;
}

export async function openArchitectureSummaryWebview(
	context: vscode.ExtensionContext,
	options: OpenArchitectureSummaryWebviewOptions
): Promise<void> {
	const panel = vscode.window.createWebviewPanel(
		'aiDev.architectureSummaryPreview',
		'Generate Architecture Summary',
		vscode.ViewColumn.One,
		{
			enableScripts: true,
			retainContextWhenHidden: true,
		}
	);

	panel.webview.html = getWebviewHtml(panel.webview, options);

	panel.webview.onDidReceiveMessage(async (message: { type?: string; previewPayload?: ArchitectureSummaryPreviewResult }) => {
		if (message.type === 'previewCandidates') {
			try {
				const preview = await options.onPreview();
				await panel.webview.postMessage({ type: 'previewResult', payload: preview });
			} catch (error) {
				const messageText = error instanceof Error ? error.message : String(error);
				await panel.webview.postMessage({ type: 'previewError', error: messageText });
			}
			return;
		}

		if (message.type === 'generateArchitectureSummary') {
			if (!options.onGenerateRequested) {
				await panel.webview.postMessage({
					type: 'generateError',
					error: 'Generate Architecture Summary is not implemented yet.',
				});
				return;
			}

			if (!message.previewPayload || !Array.isArray(message.previewPayload.items)) {
				await panel.webview.postMessage({
					type: 'generateError',
					error: 'Missing preview plan. Run Preview again.',
				});
				return;
			}

			await panel.webview.postMessage({ type: 'generateStarted' });
			await options.onGenerateRequested(message.previewPayload);
		}
	});

	context.subscriptions.push(panel);
}
