import * as vscode from 'vscode';
import {
	parseReviewFindings,
	type AssistantReport,
	type AssistantReportSection,
	type ReviewFinding,
} from './assistantReport';

function escapeHtml(value: string): string {
	return value
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&#39;');
}

function renderInlineMarkdown(value: string): string {
	return escapeHtml(value)
		.replace(/`([^`]+)`/g, '<code>$1</code>')
		.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
		.replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function renderSectionContent(content: string): string {
	const lines = content.replace(/\r\n/g, '\n').split('\n');
	const output: string[] = [];
	let listItems: string[] = [];

	const flushList = (): void => {
		if (listItems.length === 0) {
			return;
		}

		output.push(
			'<ul>',
			...listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`),
			'</ul>'
		);
		listItems = [];
	};

	for (const line of lines) {
		const trimmed = line.trim();

		if (!trimmed) {
			flushList();
			continue;
		}

		const listMatch = trimmed.match(/^[-*]\s+(.+)$/);
		if (listMatch) {
			listItems.push(listMatch[1]);
			continue;
		}

		flushList();
		output.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
	}

	flushList();
	return output.join('\n');
}

function renderSection(section: AssistantReportSection): string {
	return [
		`<section id="${escapeHtml(section.id)}">`,
		`<h2>${escapeHtml(section.title)}</h2>`,
		renderSectionContent(section.content),
		'</section>',
	].join('\n');
}

function serializeForInlineScript(value: unknown): string {
	return JSON.stringify(value)
		.replace(/</g, '\\u003c')
		.replace(/>/g, '\\u003e')
		.replace(/&/g, '\\u0026');
}

function renderReviewFindingRow(
	finding: ReviewFinding,
	index: number
): string {
	return [
		`<tr class="finding-row" data-finding-index="${index}" tabindex="0">`,
		'<td>',
		`<span class="severity severity-${escapeHtml(finding.severity)}">`,
		escapeHtml(finding.severity),
		'</span>',
		'</td>',
		`<td class="finding-title">${escapeHtml(finding.title)}</td>`,
		`<td>${escapeHtml(finding.category)}</td>`,
		`<td><code>${escapeHtml(finding.sourceFile)}</code></td>`,
		`<td><code>${escapeHtml(finding.documentationFile)}</code></td>`,
		'</tr>',
	].join('');
}

function buildReviewReportHtml(report: AssistantReport): string {
	const findings = parseReviewFindings(report.rawResponse);

	const questionHtml = report.question
		? `<div><dt>Question</dt><dd>${escapeHtml(report.question)}</dd></div>`
		: '';

	const modelHtml = report.modelName
		? `<div><dt>Model</dt><dd>${escapeHtml(report.modelName)}</dd></div>`
		: '';

	const warningHtml = report.warnings.length > 0
		? [
			'<div class="warning-box">',
			'<ul>',
			...report.warnings.map(
				(warning) => `<li>${escapeHtml(warning)}</li>`
			),
			'</ul>',
			'</div>',
		].join('')
		: '';

	const emptyState = findings.length === 0
		? [
			'<div class="empty-state">',
			'<h2>No structured findings found</h2>',
			'<p>The raw review response is still available below.</p>',
			'</div>',
		].join('')
		: '';

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8">
	<meta
		name="viewport"
		content="width=device-width, initial-scale=1.0"
	>
	<title>${escapeHtml(report.title)}</title>
	<style>
		:root {
			color-scheme: light dark;
		}

		* {
			box-sizing: border-box;
		}

		body {
			margin: 0;
			color: var(--vscode-editor-foreground);
			background: var(--vscode-editor-background);
			font-family:
				-apple-system,
				BlinkMacSystemFont,
				"Segoe UI",
				sans-serif;
			line-height: 1.45;
		}

		body.inspector-open {
			padding-bottom: var(--inspector-height, 38vh);
		}

		.review-shell {
			max-width: 1500px;
			margin: 0 auto;
			padding: 28px 32px 72px;
		}

		h1,
		h2,
		h3 {
			font-family: Georgia, "Times New Roman", serif;
			font-weight: 400;
		}

		h1 {
			margin: 0;
			padding-bottom: 8px;
			border-bottom: 1px solid var(--vscode-panel-border);
			font-size: 34px;
			line-height: 1.15;
		}

		.subtitle {
			margin: 8px 0 18px;
			color: var(--vscode-descriptionForeground);
			font-size: 13px;
		}

		.metadata {
			display: grid;
			grid-template-columns:
				repeat(auto-fit, minmax(220px, 1fr));
			gap: 8px 18px;
			margin: 0 0 22px;
			padding: 12px 14px;
			border: 1px solid var(--vscode-panel-border);
			background: var(--vscode-sideBar-background);
			font-size: 13px;
		}

		.metadata div {
			display: grid;
			grid-template-columns: 88px minmax(0, 1fr);
			gap: 10px;
		}

		.metadata dt {
			font-weight: 600;
		}

		.metadata dd {
			margin: 0;
			overflow-wrap: anywhere;
		}

		.warning-box {
			margin-bottom: 18px;
			padding: 10px 14px;
			border-left: 4px solid
				var(--vscode-notificationsWarningIcon-foreground);
			background:
				var(--vscode-inputValidation-warningBackground);
		}

		.warning-box ul {
			margin: 0;
			padding-left: 22px;
		}

		.findings-heading {
			display: flex;
			align-items: baseline;
			justify-content: space-between;
			gap: 16px;
			margin: 26px 0 10px;
		}

		.findings-heading h2 {
			margin: 0;
			font-size: 24px;
		}

		.finding-count {
			color: var(--vscode-descriptionForeground);
			font-size: 13px;
		}

		.table-shell {
			overflow: auto;
			border: 1px solid var(--vscode-panel-border);
		}

		table {
			width: 100%;
			min-width: 1050px;
			border-collapse: collapse;
			font-size: 13px;
		}

		th,
		td {
			padding: 10px 12px;
			border-bottom:
				1px solid var(--vscode-panel-border);
			text-align: left;
			vertical-align: top;
		}

		th {
			position: sticky;
			top: 0;
			z-index: 1;
			background: var(--vscode-sideBar-background);
			font-weight: 600;
		}

		.finding-row {
			cursor: pointer;
		}

		.finding-row:hover,
		.finding-row:focus {
			outline: none;
			background:
				var(--vscode-list-hoverBackground);
		}

		.finding-row.active {
			background:
				var(--vscode-list-activeSelectionBackground);
			color:
				var(--vscode-list-activeSelectionForeground);
		}

		.finding-title {
			min-width: 220px;
			font-weight: 600;
		}

		code {
			padding: 1px 4px;
			border: 1px solid var(--vscode-widget-border);
			border-radius: 3px;
			background:
				var(--vscode-textCodeBlock-background);
			font-family: var(--vscode-editor-font-family);
			font-size: 0.92em;
			overflow-wrap: anywhere;
		}

		.severity {
			display: inline-block;
			min-width: 66px;
			padding: 2px 7px;
			border-radius: 3px;
			text-align: center;
			text-transform: capitalize;
			font-size: 11px;
			font-weight: 600;
		}

		.severity-blocking {
			color: var(--vscode-errorForeground);
		}

		.severity-warning {
			color:
				var(--vscode-notificationsWarningIcon-foreground);
		}

		.severity-info {
			color:
				var(--vscode-notificationsInfoIcon-foreground);
		}

		.severity-unknown {
			color: var(--vscode-descriptionForeground);
		}

		.empty-state {
			padding: 28px;
			border: 1px dashed var(--vscode-panel-border);
			text-align: center;
		}

		.raw-response {
			margin-top: 24px;
			border: 1px solid var(--vscode-panel-border);
		}

		.raw-response summary {
			cursor: pointer;
			padding: 10px 12px;
			background: var(--vscode-sideBar-background);
			font-weight: 600;
		}

		.raw-response pre {
			margin: 0;
			padding: 16px;
			overflow-x: auto;
			white-space: pre-wrap;
			word-break: break-word;
			font-family: var(--vscode-editor-font-family);
			background:
				var(--vscode-textCodeBlock-background);
		}

		.review-inspector {
			position: fixed;
			right: 0;
			bottom: 0;
			left: 0;
			z-index: 20;
			display: none;
			height: var(--inspector-height, 38vh);
			min-height: 180px;
			max-height: 72vh;
			border-top: 1px solid var(--vscode-panel-border);
			background: var(--vscode-editor-background);
			box-shadow: 0 -8px 22px rgb(0 0 0 / 24%);
		}

		.review-inspector.open {
			display: flex;
			flex-direction: column;
		}

		.inspector-resizer {
			height: 7px;
			flex: 0 0 7px;
			cursor: ns-resize;
			background: transparent;
		}

		.inspector-resizer:hover,
		.inspector-resizer.dragging {
			background:
				var(--vscode-focusBorder);
		}

		.inspector-header {
			display: flex;
			align-items: flex-start;
			justify-content: space-between;
			gap: 18px;
			padding: 10px 18px 12px;
			border-bottom: 1px solid var(--vscode-panel-border);
			background: var(--vscode-sideBar-background);
		}

		.inspector-header h2 {
			margin: 0;
			font-size: 22px;
		}

		.inspector-close {
			padding: 3px 9px;
			border: 1px solid transparent;
			color: var(--vscode-foreground);
			background: transparent;
			cursor: pointer;
			font-size: 18px;
		}

		.inspector-close:hover {
			border-color: var(--vscode-button-border);
			background:
				var(--vscode-toolbar-hoverBackground);
		}

		.inspector-body {
			flex: 1 1 auto;
			overflow: auto;
			padding: 16px 20px 30px;
		}

		.inspector-fields {
			display: grid;
			grid-template-columns:
				repeat(auto-fit, minmax(240px, 1fr));
			gap: 10px 20px;
			margin-bottom: 20px;
		}

		.inspector-field {
			min-width: 0;
		}

		.inspector-label {
			display: block;
			margin-bottom: 3px;
			color: var(--vscode-descriptionForeground);
			font-size: 11px;
			font-weight: 600;
			letter-spacing: 0.04em;
			text-transform: uppercase;
		}

		.detail-section {
			margin-top: 18px;
		}

		.detail-section h3 {
			margin: 0 0 8px;
			padding-bottom: 4px;
			border-bottom: 1px solid var(--vscode-panel-border);
			font-size: 18px;
		}

		.detail-section p {
			margin: 0;
			white-space: pre-wrap;
		}

		.detail-section ul {
			margin: 0;
			padding-left: 24px;
		}

		.detail-section li {
			margin: 5px 0;
		}

		.hidden {
			display: none;
		}

		@media (max-width: 800px) {
			.review-shell {
				padding: 20px 18px 60px;
			}

			h1 {
				font-size: 28px;
			}
		}
	</style>
</head>
<body>
	<main class="review-shell">
		<header>
			<h1>${escapeHtml(report.title)}</h1>
			<div class="subtitle">AI Dev review report</div>

			<dl class="metadata">
				${questionHtml}
				<div>
					<dt>Route</dt>
					<dd>${escapeHtml(report.route)}</dd>
				</div>
				${modelHtml}
				<div>
					<dt>Timestamp</dt>
					<dd>${escapeHtml(report.timestamp)}</dd>
				</div>
			</dl>
		</header>

		${warningHtml}

		<div class="findings-heading">
			<h2>Findings</h2>
			<div class="finding-count">
				${findings.length} finding${findings.length === 1 ? '' : 's'}
			</div>
		</div>

		${emptyState}

		${findings.length > 0 ? `
		<div class="table-shell">
			<table aria-label="Review findings">
				<thead>
					<tr>
						<th>Severity</th>
						<th>Finding</th>
						<th>Category</th>
						<th>Source</th>
						<th>Documentation</th>
					</tr>
				</thead>
				<tbody>
					${findings
						.map(renderReviewFindingRow)
						.join('')}
				</tbody>
			</table>
		</div>` : ''}

		<details class="raw-response">
			<summary>Show raw review response</summary>
			<pre>${escapeHtml(report.rawResponse)}</pre>
		</details>
	</main>

	<aside
		id="review-inspector"
		class="review-inspector"
		aria-label="Selected finding details"
	>
		<div
			id="inspector-resizer"
			class="inspector-resizer"
			title="Drag to resize"
		></div>

		<div class="inspector-header">
			<div>
				<h2 id="detail-title">Finding details</h2>
			</div>
			<button
				id="inspector-close"
				class="inspector-close"
				type="button"
				aria-label="Close finding details"
			>×</button>
		</div>

		<div class="inspector-body">
			<div class="inspector-fields">
				<div class="inspector-field">
					<span class="inspector-label">Severity</span>
					<span
						id="detail-severity"
						class="severity severity-unknown"
					></span>
				</div>
				<div class="inspector-field">
					<span class="inspector-label">Category</span>
					<span id="detail-category"></span>
				</div>
				<div class="inspector-field">
					<span class="inspector-label">Source</span>
					<code id="detail-source"></code>
				</div>
				<div class="inspector-field">
					<span class="inspector-label">Documentation</span>
					<code id="detail-documentation"></code>
				</div>
				<div class="inspector-field">
					<span class="inspector-label">Origin</span>
					<span id="detail-origin"></span>
				</div>
			</div>

			<section id="detail-evidence-section" class="detail-section">
				<h3>Evidence</h3>
				<ul id="detail-evidence"></ul>
			</section>

			<section id="detail-impact-section" class="detail-section">
				<h3>Impact</h3>
				<p id="detail-impact"></p>
			</section>

			<section id="detail-action-section" class="detail-section">
				<h3>Suggested action</h3>
				<p id="detail-action"></p>
			</section>

			<section id="detail-ai-section" class="detail-section">
				<h3>AI-generated update appropriate?</h3>
				<p id="detail-ai"></p>
			</section>

			<section id="detail-uncertainty-section" class="detail-section">
				<h3>Uncertainty</h3>
				<p id="detail-uncertainty"></p>
			</section>
		</div>
	</aside>

	<script>
		const findings = ${serializeForInlineScript(findings)};
		const rows = Array.from(
			document.querySelectorAll('.finding-row')
		);
		const inspector =
			document.getElementById('review-inspector');
		const closeButton =
			document.getElementById('inspector-close');
		const resizer =
			document.getElementById('inspector-resizer');

		const setText = (id, value) => {
			const element = document.getElementById(id);
			if (element) {
				element.textContent = value || '';
			}
		};

		const setSectionVisibility = (id, visible) => {
			const element = document.getElementById(id);
			element?.classList.toggle('hidden', !visible);
		};

		const openFinding = (index) => {
			const finding = findings[index];
			if (!finding) {
				return;
			}

			rows.forEach((row, rowIndex) => {
				row.classList.toggle('active', rowIndex === index);
			});

			setText('detail-title', finding.title);
			setText('detail-severity', finding.severity);

			const severityElement =
				document.getElementById('detail-severity');
			if (severityElement) {
				severityElement.className =
					'severity severity-' + finding.severity;
			}

			setText('detail-category', finding.category);
			setText('detail-source', finding.sourceFile);
			setText(
				'detail-documentation',
				finding.documentationFile
			);
			setText('detail-origin', finding.origin);

			const evidenceList =
				document.getElementById('detail-evidence');
			if (evidenceList) {
				evidenceList.replaceChildren();
				for (const evidence of finding.evidence) {
					const item = document.createElement('li');
					item.textContent = evidence;
					evidenceList.appendChild(item);
				}
			}

			setText('detail-impact', finding.impact);
			setText(
				'detail-action',
				finding.suggestedAction
			);
			setText(
				'detail-ai',
				finding.aiGeneratedUpdateAppropriate
			);
			setText(
				'detail-uncertainty',
				finding.uncertainty
			);

			setSectionVisibility(
				'detail-evidence-section',
				finding.evidence.length > 0
			);
			setSectionVisibility(
				'detail-impact-section',
				Boolean(finding.impact)
			);
			setSectionVisibility(
				'detail-action-section',
				Boolean(finding.suggestedAction)
			);
			setSectionVisibility(
				'detail-ai-section',
				Boolean(
					finding.aiGeneratedUpdateAppropriate
				)
			);
			setSectionVisibility(
				'detail-uncertainty-section',
				Boolean(finding.uncertainty)
			);

			inspector?.classList.add('open');
			document.body.classList.add('inspector-open');
		};

		const closeInspector = () => {
			inspector?.classList.remove('open');
			document.body.classList.remove('inspector-open');
			rows.forEach((row) =>
				row.classList.remove('active')
			);
		};

		rows.forEach((row) => {
			const index = Number(row.dataset.findingIndex);

			row.addEventListener('click', () =>
				openFinding(index)
			);

			row.addEventListener('keydown', (event) => {
				if (
					event.key === 'Enter'
					|| event.key === ' '
				) {
					event.preventDefault();
					openFinding(index);
				}
			});
		});

		closeButton?.addEventListener(
			'click',
			closeInspector
		);

		let dragStartY = 0;
		let dragStartHeight = 0;

		const handlePointerMove = (event) => {
			const nextHeight =
				dragStartHeight
				+ (dragStartY - event.clientY);

			const boundedHeight = Math.min(
				window.innerHeight * 0.72,
				Math.max(180, nextHeight)
			);

			document.documentElement.style.setProperty(
				'--inspector-height',
				\`\${boundedHeight}px\`
			);
		};

		const stopResize = () => {
			resizer?.classList.remove('dragging');
			window.removeEventListener(
				'pointermove',
				handlePointerMove
			);
			window.removeEventListener(
				'pointerup',
				stopResize
			);
		};

		resizer?.addEventListener(
			'pointerdown',
			(event) => {
				dragStartY = event.clientY;
				dragStartHeight =
					inspector?.getBoundingClientRect()
						.height ?? 0;

				resizer.classList.add('dragging');

				window.addEventListener(
					'pointermove',
					handlePointerMove
				);
				window.addEventListener(
					'pointerup',
					stopResize
				);
			}
		);

		window.addEventListener('keydown', (event) => {
			if (event.key === 'Escape') {
				closeInspector();
			}
		});
	</script>
</body>
</html>`;
}

export function buildAssistantReportHtml(report: AssistantReport): string {
	if (report.route === 'review') {
		return buildReviewReportHtml(report);
	}

	const contentSections = report.sections.length > 0
		? report.sections
		: [{
			id: 'answer',
			title: 'Answer',
			content: report.answer,
		}];

	const tableOfContents = [
		...report.warnings.length > 0
			? [{ id: 'warnings', title: 'Warnings' }]
			: [],
		...contentSections.map((section) => ({
			id: section.id,
			title: section.title,
		})),
		{ id: 'raw-response', title: 'Raw response' },
	];

	const warningHtml = report.warnings.length > 0
		? [
			'<section id="warnings">',
			'<h2>Warnings</h2>',
			'<div class="warning-box">',
			'<ul>',
			...report.warnings.map(
				(warning) => `<li>${escapeHtml(warning)}</li>`
			),
			'</ul>',
			'</div>',
			'</section>',
		].join('\n')
		: '';

	const questionHtml = report.question
		? `<div><dt>Question</dt><dd>${escapeHtml(report.question)}</dd></div>`
		: '';

	const modelHtml = report.modelName
		? `<div><dt>Model</dt><dd>${escapeHtml(report.modelName)}</dd></div>`
		: '';

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8">
	<meta
		name="viewport"
		content="width=device-width, initial-scale=1.0"
	>
	<title>${escapeHtml(report.title)}</title>
	<style>
		:root {
			color-scheme: light dark;
		}

		html {
			scroll-behavior: smooth;
		}

		body {
			margin: 0;
			color: var(--vscode-editor-foreground);
			background: var(--vscode-editor-background);
			font-family:
				-apple-system,
				BlinkMacSystemFont,
				"Segoe UI",
				sans-serif;
			line-height: 1.6;
		}

		.layout {
			display: grid;
			grid-template-columns: 250px minmax(0, 860px);
			gap: 48px;
			max-width: 1180px;
			margin: 0 auto;
			padding: 36px 40px 80px;
		}

		.toc {
			position: sticky;
			top: 24px;
			align-self: start;
			max-height: calc(100vh - 48px);
			overflow-y: auto;
			padding-right: 20px;
			border-right: 1px solid var(--vscode-panel-border);
		}

		.toc h2 {
			margin: 0 0 12px;
			padding-bottom: 8px;
			border-bottom: 1px solid var(--vscode-panel-border);
			font-family: Georgia, "Times New Roman", serif;
			font-size: 17px;
			font-weight: 600;
		}

		.toc ol {
			margin: 0;
			padding-left: 24px;
		}

		.toc li {
			margin: 6px 0;
			padding-left: 3px;
		}

		.toc a {
			color: var(--vscode-textLink-foreground);
			text-decoration: none;
		}

		.toc a:hover {
			text-decoration: underline;
		}

		.article {
			min-width: 0;
		}

		h1,
		h2 {
			font-family: Georgia, "Times New Roman", serif;
			font-weight: 400;
		}

		h1 {
			margin: 0;
			padding-bottom: 8px;
			border-bottom: 1px solid var(--vscode-panel-border);
			font-size: 38px;
			line-height: 1.15;
		}

		h2 {
			margin: 42px 0 18px;
			padding-bottom: 6px;
			border-bottom: 1px solid var(--vscode-panel-border);
			font-size: 27px;
			line-height: 1.2;
		}

		p {
			margin: 0 0 16px;
		}

		ul {
			margin-top: 8px;
			padding-left: 28px;
		}

		li {
			margin: 5px 0;
		}

		code {
			padding: 1px 4px;
			border: 1px solid var(--vscode-widget-border);
			border-radius: 3px;
			background: var(--vscode-textCodeBlock-background);
			font-family: var(--vscode-editor-font-family);
			font-size: 0.92em;
		}

		.subtitle {
			margin: 8px 0 22px;
			color: var(--vscode-descriptionForeground);
			font-size: 13px;
		}

		.metadata {
			display: grid;
			gap: 8px;
			margin: 0 0 28px;
			padding: 14px 16px;
			border: 1px solid var(--vscode-panel-border);
			background: var(--vscode-sideBar-background);
			font-size: 13px;
		}

		.metadata div {
			display: grid;
			grid-template-columns: 110px minmax(0, 1fr);
			gap: 12px;
		}

		.metadata dt {
			font-weight: 600;
		}

		.metadata dd {
			margin: 0;
			overflow-wrap: anywhere;
		}

		.warning-box {
			padding: 12px 16px;
			border-left: 4px solid var(--vscode-notificationsWarningIcon-foreground);
			background: var(--vscode-inputValidation-warningBackground);
		}

		.warning-box ul {
			margin: 0;
		}

		details {
			margin-top: 16px;
			border: 1px solid var(--vscode-panel-border);
		}

		summary {
			cursor: pointer;
			padding: 10px 12px;
			background: var(--vscode-sideBar-background);
			font-weight: 600;
		}

		pre {
			margin: 0;
			padding: 16px;
			overflow-x: auto;
			white-space: pre-wrap;
			word-break: break-word;
			font-family: var(--vscode-editor-font-family);
			font-size: var(--vscode-editor-font-size);
			background: var(--vscode-textCodeBlock-background);
		}

		section {
			scroll-margin-top: 24px;
		}

		@media (max-width: 800px) {
			.layout {
				display: block;
				padding: 24px;
			}

			.toc {
				position: static;
				max-height: none;
				margin-bottom: 36px;
				padding: 0 0 20px;
				border-right: 0;
				border-bottom: 1px solid var(--vscode-panel-border);
			}
		}
	</style>
</head>
<body>
	<div class="layout">
		<nav class="toc" aria-label="Contents">
			<h2>Contents</h2>
			<ol>
				${tableOfContents.map((item) => `
				<li>
					<a href="#${escapeHtml(item.id)}">
						${escapeHtml(item.title)}
					</a>
				</li>`).join('')}
			</ol>
		</nav>

		<main class="article">
			<header>
				<h1>${escapeHtml(report.title)}</h1>
				<div class="subtitle">AI Dev operation report</div>

				<dl class="metadata">
					${questionHtml}
					<div>
						<dt>Route</dt>
						<dd>${escapeHtml(report.route)}</dd>
					</div>
					${modelHtml}
					<div>
						<dt>Timestamp</dt>
						<dd>${escapeHtml(report.timestamp)}</dd>
					</div>
				</dl>
			</header>

			${warningHtml}

			${contentSections.map(renderSection).join('\n')}

			<section id="raw-response">
				<h2>Raw response</h2>
				<details>
					<summary>Show raw model response</summary>
					<pre>${escapeHtml(report.rawResponse)}</pre>
				</details>
			</section>
		</main>
	</div>
</body>
</html>`;
}

export class AssistantReportPanel implements vscode.Disposable {
	private panel: vscode.WebviewPanel | undefined;
	private latestReport: AssistantReport | undefined;

	show(report: AssistantReport): void {
		this.latestReport = report;

		if (this.panel) {
			this.panel.title = report.title;
			this.panel.webview.html = buildAssistantReportHtml(report);
			this.panel.reveal(vscode.ViewColumn.Active);
			return;
		}

		this.panel = vscode.window.createWebviewPanel(
			'aiDev.report',
			report.title,
			vscode.ViewColumn.Active,
			{
				enableScripts: true,
				retainContextWhenHidden: true,
			}
		);

		this.panel.webview.html = buildAssistantReportHtml(report);
		this.panel.onDidDispose(() => {
			this.panel = undefined;
		});
	}

	refresh(): void {
		if (!this.panel || !this.latestReport) {
			return;
		}

		this.panel.title = this.latestReport.title;
		this.panel.webview.html = buildAssistantReportHtml(
			this.latestReport
		);
	}

	dispose(): void {
		this.panel?.dispose();
		this.panel = undefined;
		this.latestReport = undefined;
	}
}
