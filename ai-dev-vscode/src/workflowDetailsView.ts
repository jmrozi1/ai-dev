import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { readAiDevConfig, resolveAiDevCorePath } from './config';
import { getOpenWorkspaceRoot } from './workspace';
import { AI_DEV_WORKFLOWS, getWorkflowById, type AiDevWorkflow } from './workflows';

interface WorkflowDetailSection {
	label: string;
	pathLabel: string;
	content: string;
	missing: boolean;
}

function escapeHtml(text: string): string {
	return text
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}

function createNonce(): string {
	const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
	let nonce = '';
	for (let i = 0; i < 32; i += 1) {
		nonce += chars.charAt(Math.floor(Math.random() * chars.length));
	}
	return nonce;
}

async function readWorkflowSections(workflow: AiDevWorkflow): Promise<WorkflowDetailSection[]> {
	if (workflow.instructionFiles.length === 0) {
		return [];
	}

	const workspaceRoot = getOpenWorkspaceRoot();
	if (!workspaceRoot) {
		return [
			{
				label: 'Instructions',
				pathLabel: 'N/A',
				content: 'Unable to load workflow instructions. Missing .ai-dev.yaml or aiDevCore.path.',
				missing: true,
			},
		];
	}

	let aiDevConfig;
	try {
		aiDevConfig = await readAiDevConfig(workspaceRoot);
	} catch {
		return [
			{
				label: 'Instructions',
				pathLabel: 'N/A',
				content: 'Unable to load workflow instructions. Missing .ai-dev.yaml or aiDevCore.path.',
				missing: true,
			},
		];
	}

	if (!aiDevConfig.aiDevCorePathFromYaml) {
		return [
			{
				label: 'Instructions',
				pathLabel: 'N/A',
				content: 'Unable to load workflow instructions. Missing .ai-dev.yaml or aiDevCore.path.',
				missing: true,
			},
		];
	}

	const resolvedCorePath = resolveAiDevCorePath(workspaceRoot, aiDevConfig.aiDevCorePathFromYaml);
	const sections: WorkflowDetailSection[] = [];

	for (const instructionFile of workflow.instructionFiles) {
		const resolvedFilePath = path.join(resolvedCorePath, ...instructionFile.relativePath.split('/'));
		try {
			const content = await fs.readFile(resolvedFilePath, 'utf8');
			sections.push({
				label: instructionFile.label,
				pathLabel: instructionFile.relativePath,
				content,
				missing: false,
			});
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
				sections.push({
					label: instructionFile.label,
					pathLabel: instructionFile.relativePath,
					content: `Missing instruction file: ${instructionFile.relativePath}`,
					missing: true,
				});
				continue;
			}

			sections.push({
				label: instructionFile.label,
				pathLabel: instructionFile.relativePath,
				content: `Unable to read instruction file: ${instructionFile.relativePath}`,
				missing: true,
			});
		}
	}

	return sections;
}

export class WorkflowDetailsViewProvider implements vscode.WebviewViewProvider {
	private view: vscode.WebviewView | undefined;
	private activeWorkflowId = AI_DEV_WORKFLOWS[0]?.id;

	async resolveWebviewView(webviewView: vscode.WebviewView): Promise<void> {
		this.view = webviewView;
		webviewView.webview.options = {
			enableScripts: true,
		};

		webviewView.webview.onDidReceiveMessage(async (message: { type?: string; workflowId?: string }) => {
			if (message.type !== 'runWorkflow' || !message.workflowId) {
				return;
			}

			const workflow = getWorkflowById(message.workflowId);
			if (!workflow) {
				return;
			}

			await vscode.commands.executeCommand(workflow.commandId);
		});

		await this.render();
	}

	async setActiveWorkflow(workflowId: string): Promise<void> {
		if (!getWorkflowById(workflowId)) {
			return;
		}

		this.activeWorkflowId = workflowId;
		await this.render();
	}

	private async render(): Promise<void> {
		if (!this.view) {
			return;
		}

		const workflow = getWorkflowById(this.activeWorkflowId ?? '') ?? AI_DEV_WORKFLOWS[0];
		if (!workflow) {
			this.view.webview.html = '<!DOCTYPE html><html><body>No workflows available.</body></html>';
			return;
		}

		const sections = await readWorkflowSections(workflow);
		this.view.webview.html = this.getHtml(this.view.webview, workflow, sections);
	}

	private getHtml(webview: vscode.Webview, workflow: AiDevWorkflow, sections: WorkflowDetailSection[]): string {
		const nonce = createNonce();
		const instructionsHtml = sections
			.map((section) => {
				const status = section.missing ? '<p><em>Unavailable</em></p>' : '';
				return `
				<section>
					<h3>${escapeHtml(section.label)}</h3>
					<p><strong>Path:</strong> ${escapeHtml(section.pathLabel)}</p>
					${status}
					<pre>${escapeHtml(section.content)}</pre>
				</section>
			`;
			})
			.join('\n');

		return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<title>${escapeHtml(workflow.title)}</title>
	<style>
		body {
			font-family: var(--vscode-font-family);
			font-size: var(--vscode-font-size);
			color: var(--vscode-foreground);
			padding: 12px;
		}
		h1, h2, h3 {
			margin: 0 0 8px 0;
		}
		.section {
			margin-bottom: 16px;
		}
		button {
			padding: 6px 12px;
			cursor: pointer;
		}
		pre {
			white-space: pre-wrap;
			word-break: break-word;
			padding: 8px;
			border: 1px solid var(--vscode-editorWidget-border);
			background: var(--vscode-editor-background);
		}
	</style>
</head>
<body>
	<div class="section">
		<h2>${escapeHtml(workflow.title)}</h2>
		<p>${escapeHtml(workflow.description)}</p>
		<p><strong>Usage:</strong> ${escapeHtml(workflow.usage)}</p>
		<button id="runWorkflow" type="button">${escapeHtml(workflow.buttonLabel)}</button>
	</div>
	<div class="section">
		${instructionsHtml}
	</div>
	<script nonce="${nonce}">
		const vscode = acquireVsCodeApi();
		const button = document.getElementById('runWorkflow');
		if (button) {
			button.addEventListener('click', () => {
				vscode.postMessage({ type: 'runWorkflow', workflowId: ${JSON.stringify(workflow.id)} });
			});
		}
	</script>
</body>
</html>`;
	}
}
