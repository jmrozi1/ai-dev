import * as vscode from 'vscode';
import type {
	AssistantReport,
	AssistantReportSection,
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

export function buildAssistantReportHtml(report: AssistantReport): string {
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
				enableScripts: false,
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
