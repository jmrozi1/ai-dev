import * as vscode from 'vscode';
import {
	createDefaultSummarizationConfig,
	type SummarizationConfig,
	type SummarizationRule,
	validateSummarizationConfig,
	validateSummarizationGlobSyntax,
} from './summarizationConfig';

export interface SummarizationPatternTestResult {
	totalMatches: number;
	previewPaths: string[];
	omittedCount: number;
}

export interface SummarizationConfigPanelOptions {
	load: () => Promise<SummarizationConfig>;
	save: (config: SummarizationConfig) => Promise<void>;
	testPattern: (
		glob: string
	) => Promise<SummarizationPatternTestResult>;
}

function escapeHtml(value: string): string {
	return value
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&#39;');
}

function serializeForScript(value: unknown): string {
	return JSON.stringify(value)
		.replace(/</g, '\\u003c')
		.replace(/>/g, '\\u003e')
		.replace(/&/g, '\\u0026');
}

export function buildSummarizationConfigHtml(
	config: SummarizationConfig
): string {
	const safeConfig = serializeForScript(config);

	return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8">
	<meta
		name="viewport"
		content="width=device-width, initial-scale=1.0"
	>
	<title>Summarization Configuration</title>
	<style>
		:root {
			color-scheme: light dark;
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

		main {
			max-width: 1180px;
			margin: 0 auto;
			padding: 34px 40px 80px;
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
			font-size: 36px;
		}

		.intro {
			max-width: 820px;
			margin: 12px 0 26px;
			color: var(--vscode-descriptionForeground);
		}

		.toolbar {
			display: flex;
			align-items: center;
			gap: 10px;
			margin: 0 0 14px;
		}

		button {
			padding: 7px 13px;
			border: 1px solid
				var(--vscode-button-border, transparent);
			background: var(--vscode-button-background);
			color: var(--vscode-button-foreground);
			cursor: pointer;
		}

		button.secondary {
			background:
				var(--vscode-button-secondaryBackground);
			color:
				var(--vscode-button-secondaryForeground);
		}

		button.danger {
			background:
				var(--vscode-inputValidation-errorBackground);
			color: var(--vscode-errorForeground);
			border-color:
				var(--vscode-inputValidation-errorBorder);
		}

		button:disabled {
			opacity: 0.55;
			cursor: not-allowed;
		}

		#status {
			min-height: 20px;
			margin-left: auto;
			font-size: 13px;
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

		.table-frame {
			overflow-x: auto;
			border: 1px solid var(--vscode-panel-border);
		}

		table {
			width: 100%;
			border-collapse: collapse;
			table-layout: fixed;
		}

		th,
		td {
			padding: 9px 10px;
			border-bottom: 1px solid var(--vscode-panel-border);
			text-align: left;
			vertical-align: top;
		}

		th {
			background: var(--vscode-sideBar-background);
			font-weight: 600;
		}

		tbody tr {
			cursor: default;
		}

		tbody tr:hover,
		tbody tr.selected {
			background: var(--vscode-list-hoverBackground);
		}

		.priority-column {
			width: 72px;
		}

		.status-column {
			width: 92px;
		}

		.pattern-column {
			width: 235px;
		}

		.name-column {
			width: 205px;
		}

		.clip {
			display: -webkit-box;
			overflow: hidden;
			-webkit-box-orient: vertical;
			-webkit-line-clamp: 2;
			line-clamp: 2;
			color: var(--vscode-descriptionForeground);
		}

		code {
			font-family: var(--vscode-editor-font-family);
			font-size: 0.92em;
			overflow-wrap: anywhere;
		}

		.badge {
			display: inline-block;
			padding: 4px 10px;
			border-radius: 4px;
			background: var(--vscode-badge-background);
			color: var(--vscode-badge-foreground);
			font-size: 12px;
			line-height: 16px;
			font-weight: 400;
			letter-spacing: 0;
		}

		.badge.disabled {
			background: var(--vscode-sideBar-background);
			color: var(--vscode-descriptionForeground);
		}

		.general-row {
			border-left: 4px solid
				var(--vscode-textLink-foreground);
		}

		dialog {
			width: min(720px, calc(100vw - 48px));
			max-height: calc(100vh - 48px);
			padding: 0;
			border: 1px solid var(--vscode-panel-border);
			background: var(--vscode-editor-background);
			color: var(--vscode-editor-foreground);
			box-shadow: 0 12px 36px rgba(0, 0, 0, 0.35);
		}

		dialog::backdrop {
			background: rgba(0, 0, 0, 0.48);
		}

		.modal-header {
			padding: 18px 20px 12px;
			border-bottom: 1px solid var(--vscode-panel-border);
		}

		.modal-header h2 {
			margin: 0;
			font-size: 26px;
		}

		.modal-body {
			display: grid;
			gap: 15px;
			padding: 18px 20px;
			overflow-y: auto;
		}

		.field {
			display: grid;
			gap: 6px;
		}

		.field label {
			font-weight: 600;
		}

		.field-note {
			color: var(--vscode-descriptionForeground);
			font-size: 12px;
		}

		input[type="text"],
		input[type="number"],
		textarea {
			box-sizing: border-box;
			width: 100%;
			padding: 8px 10px;
			border: 1px solid
				var(--vscode-input-border, transparent);
			background: var(--vscode-input-background);
			color: var(--vscode-input-foreground);
			font: inherit;
		}

		textarea {
			min-height: 220px;
			resize: vertical;
			font-family: var(--vscode-editor-font-family);
		}

		.checkbox-row {
			display: flex;
			align-items: center;
			gap: 8px;
		}

		.dependency-section {
			display: grid;
			gap: 14px;
			padding: 14px;
			border: 1px solid var(--vscode-panel-border);
			background: var(--vscode-sideBar-background);
		}

		.dependency-fields {
			display: grid;
			grid-template-columns:
				minmax(0, 1fr)
				minmax(120px, 0.35fr)
				minmax(140px, 0.45fr);
			gap: 12px;
		}

		.dependency-fields[hidden] {
			display: none;
		}

		@media (max-width: 720px) {
			.dependency-fields {
				grid-template-columns: 1fr;
			}
		}

		.modal-actions {
			display: flex;
			gap: 10px;
			padding: 14px 20px 18px;
			border-top: 1px solid var(--vscode-panel-border);
		}

		.modal-actions .spacer {
			flex: 1;
		}

		.field-heading {
			display: flex;
			align-items: center;
			justify-content: space-between;
			gap: 16px;
		}

		.test-checkbox {
			display: inline-flex;
			align-items: center;
			gap: 6px;
			font-weight: 400;
			white-space: nowrap;
		}

		.pattern-test-summary {
			min-height: 18px;
			color: var(--vscode-descriptionForeground);
			font-size: 13px;
		}

		.pattern-test-summary.error {
			color: var(--vscode-errorForeground);
		}

		.pattern-match-toggle {
			justify-self: start;
			padding: 0;
			border: 0;
			background: transparent;
			color: var(--vscode-textLink-foreground);
			font-size: 13px;
		}

		.pattern-match-toggle:hover {
			text-decoration: underline;
		}

		.pattern-test-matches {
			max-height: 190px;
			overflow-y: auto;
			padding: 8px 10px;
			border: 1px solid var(--vscode-panel-border);
			background: var(--vscode-textCodeBlock-background);
			font-family: var(--vscode-editor-font-family);
			font-size: 12px;
		}

		.pattern-test-matches ul {
			margin: 0;
			padding-left: 22px;
		}

		.pattern-test-matches li {
			margin: 3px 0;
		}

		.pattern-test-matches .omitted {
			margin-top: 8px;
			color: var(--vscode-descriptionForeground);
		}

		.empty {
			padding: 24px;
			color: var(--vscode-descriptionForeground);
			text-align: center;
		}
	</style>
</head>
<body>
	<main>
		<h1>Summarization Configuration</h1>
		<p class="intro">
			General instructions apply to every summarized file.
			Enabled specialized rules are added when their glob matches,
			in ascending priority order.
		</p>

		<div class="toolbar">
			<button id="addRuleButton" type="button">
				Add rule
			</button>
			<button
				id="editRuleButton"
				type="button"
				class="secondary"
				disabled
			>
				Edit
			</button>
			<span id="status" class="info"></span>
		</div>

		<div class="table-frame">
			<table>
				<thead>
					<tr>
						<th class="priority-column">Priority</th>
						<th class="pattern-column">Pattern</th>
						<th class="name-column">Name</th>
						<th>Instructions</th>
						<th class="status-column">Status</th>
					</tr>
				</thead>
				<tbody id="rulesBody"></tbody>
			</table>
		</div>
	</main>

	<dialog id="ruleDialog">
		<div class="modal-header">
			<h2 id="dialogTitle">Edit rule</h2>
		</div>

		<div class="modal-body">
			<div class="field" id="nameField">
				<label for="ruleName">Name</label>
				<input id="ruleName" type="text">
			</div>

			<div class="field" id="globField">
				<div class="field-heading">
					<label for="ruleGlob">Glob pattern</label>
					<label class="test-checkbox">
						<input id="testPatternEnabled" type="checkbox">
						<span>Test</span>
					</label>
				</div>

				<input id="ruleGlob" type="text">

				<div
					id="patternTestSummary"
					class="pattern-test-summary"
					hidden
				></div>

				<button
					id="togglePatternMatches"
					type="button"
					class="pattern-match-toggle"
					hidden
				>
					Show matching files ▸
				</button>

				<div
					id="patternTestMatches"
					class="pattern-test-matches"
					hidden
				></div>
			</div>

			<div class="field" id="priorityField">
				<label for="rulePriority">Priority</label>
				<input id="rulePriority" type="number">
				<div class="field-note">
					Lower values apply first. Higher-priority matching
					rules appear later in the final prompt.
				</div>
			</div>

			<label class="checkbox-row" id="enabledField">
				<input id="ruleEnabled" type="checkbox">
				<span>Enabled</span>
			</label>

			<section
				id="dependencySection"
				class="dependency-section"
			>
				<label class="checkbox-row">
					<input
						id="dependencyEnabled"
						type="checkbox"
					>
					<span>Enable dependency context</span>
				</label>

				<div
					id="dependencyFields"
					class="dependency-fields"
					hidden
				>
					<div class="field">
						<label for="dependencyKinds">
							Relationship kinds
						</label>
						<input
							id="dependencyKinds"
							type="text"
							placeholder="jenkins-pipeline-script"
						>
						<div class="field-note">
							Comma-separated dependency edge kinds.
						</div>
					</div>

					<div class="field">
						<label for="dependencyMaxFiles">
							Maximum files
						</label>
						<input
							id="dependencyMaxFiles"
							type="number"
							min="1"
						>
					</div>

					<div class="field">
						<label for="dependencyMaxChars">
							Maximum characters
						</label>
						<input
							id="dependencyMaxChars"
							type="number"
							min="1"
						>
					</div>

					<label class="checkbox-row">
						<input
							id="dependencyIncludeInferred"
							type="checkbox"
						>
						<span>Include inferred matches</span>
					</label>

					<div class="field-note">
						Traversal is currently limited to direct
						dependencies only.
					</div>
				</div>
			</section>

			<div class="field">
				<label for="ruleInstructions">Instructions</label>
				<textarea id="ruleInstructions"></textarea>
			</div>
		</div>


		<div class="modal-actions">
			<button
				id="deleteRuleButton"
				type="button"
				class="danger"
			>
				Delete
			</button>
			<div class="spacer"></div>
			<button
				id="cancelRuleButton"
				type="button"
				class="secondary"
			>
				Cancel
			</button>
			<button id="saveRuleButton" type="button">
				Save
			</button>
		</div>
	</dialog>

	<script>
		const vscode = acquireVsCodeApi();
		let config = ${safeConfig};
		let selectedKey = undefined;
		let editingKey = undefined;
		let saveInFlight = false;

		const body = document.getElementById('rulesBody');
		const status = document.getElementById('status');
		const addButton = document.getElementById('addRuleButton');
		const editButton = document.getElementById('editRuleButton');
		const dialog = document.getElementById('ruleDialog');
		const dialogTitle = document.getElementById('dialogTitle');
		const nameField = document.getElementById('nameField');
		const globField = document.getElementById('globField');
		const priorityField =
			document.getElementById('priorityField');
		const enabledField =
			document.getElementById('enabledField');
		const nameInput = document.getElementById('ruleName');
		const globInput = document.getElementById('ruleGlob');
		const priorityInput =
			document.getElementById('rulePriority');
		const enabledInput =
			document.getElementById('ruleEnabled');
		const instructionsInput =
			document.getElementById('ruleInstructions');
		const dependencySection =
			document.getElementById('dependencySection');
		const dependencyEnabled =
			document.getElementById('dependencyEnabled');
		const dependencyFields =
			document.getElementById('dependencyFields');
		const dependencyKinds =
			document.getElementById('dependencyKinds');
		const dependencyMaxFiles =
			document.getElementById('dependencyMaxFiles');
		const dependencyMaxChars =
			document.getElementById('dependencyMaxChars');
		const dependencyIncludeInferred =
			document.getElementById(
				'dependencyIncludeInferred'
			);
		const deleteButton =
			document.getElementById('deleteRuleButton');
		const cancelButton =
			document.getElementById('cancelRuleButton');
		const saveButton =
			document.getElementById('saveRuleButton');
		const testPatternEnabled =
			document.getElementById('testPatternEnabled');
		const patternTestSummary =
			document.getElementById('patternTestSummary');
		const togglePatternMatches =
			document.getElementById('togglePatternMatches');
		const patternTestMatches =
			document.getElementById('patternTestMatches');

		let patternTestTimer = undefined;
		let patternTestRequestId = 0;
		let patternMatchesExpanded = false;

		function escapeText(value) {
			return String(value ?? '');
		}

		function setStatus(message, tone) {
			status.textContent = message;
			status.className = tone;
		}

		function getRows() {
			return [
				{
					key: 'general',
					isGeneral: true,
					priority: 0,
					glob: '*',
					name: 'General summarization',
					instructions: config.generalInstructions,
					enabled: true,
				},
				...config.rules.map((rule) => ({
					key: rule.id,
					isGeneral: false,
					...rule,
				})),
			];
		}

		function render() {
			const rows = getRows();

			if (rows.length === 0) {
				body.innerHTML =
					'<tr><td colspan="5" class="empty">'
					+ 'No rules configured.'
					+ '</td></tr>';
				return;
			}

			body.textContent = '';

			for (const row of rows) {
				const tr = document.createElement('tr');
				tr.dataset.key = row.key;

				if (row.isGeneral) {
					tr.classList.add('general-row');
				}

				if (selectedKey === row.key) {
					tr.classList.add('selected');
				}

				const priority = document.createElement('td');
				priority.textContent = String(row.priority);

				const pattern = document.createElement('td');
				const patternCode = document.createElement('code');
				patternCode.textContent = row.glob;
				pattern.appendChild(patternCode);

				const name = document.createElement('td');
				name.textContent = row.name;

				const instructions = document.createElement('td');
				const instructionPreview =
					document.createElement('div');
				instructionPreview.className = 'clip';
				instructionPreview.textContent = row.instructions;
				instructions.appendChild(instructionPreview);

				const state = document.createElement('td');
				const badge = document.createElement('span');
				badge.className =
					row.enabled ? 'badge' : 'badge disabled';
				badge.textContent =
					row.enabled ? 'Enabled' : 'Disabled';
				state.appendChild(badge);

				tr.append(
					priority,
					pattern,
					name,
					instructions,
					state
				);

				tr.addEventListener('click', () => {
					selectedKey = row.key;
					editButton.disabled = false;
					render();
				});

				tr.addEventListener('dblclick', () => {
					openEditor(row.key);
				});

				tr.addEventListener('keydown', (event) => {
					if (event.key === 'Enter') {
						openEditor(row.key);
					}
				});

				tr.tabIndex = 0;
				body.appendChild(tr);
			}
		}

		function findRule(key) {
			return config.rules.find((rule) => rule.id === key);
		}

		function updateDependencyFieldsVisibility() {
			dependencyFields.hidden =
				!dependencyEnabled.checked;
		}

		function openEditor(key) {
			editingKey = key;

			const isGeneral = key === 'general';
			const rule = isGeneral
				? {
					name: 'General summarization',
					glob: '*',
					priority: 0,
					enabled: true,
					instructions: config.generalInstructions,
				}
				: findRule(key);

			if (!rule) {
				return;
			}

			dialogTitle.textContent = isGeneral
				? 'Edit general instructions'
				: 'Edit summarization rule';

			nameField.hidden = isGeneral;
			globField.hidden = isGeneral;
			priorityField.hidden = isGeneral;
			enabledField.hidden = isGeneral;
			dependencySection.hidden = isGeneral;
			deleteButton.hidden = isGeneral;
			testPatternEnabled.parentElement.hidden = isGeneral;
			testPatternEnabled.checked = false;
			patternMatchesExpanded = false;
			clearPatternTestDisplay();

			nameInput.value = rule.name;
			globInput.value = rule.glob;
			priorityInput.value = String(rule.priority);
			enabledInput.checked = !!rule.enabled;
			instructionsInput.value = rule.instructions;

			const dependencyStrategy =
				rule.dependencyStrategy;

			dependencyEnabled.checked =
				!!dependencyStrategy;
			dependencyKinds.value =
				dependencyStrategy
					? dependencyStrategy.follow.join(', ')
					: 'jenkins-pipeline-script';
			dependencyMaxFiles.value = String(
				dependencyStrategy?.maxFiles ?? 4
			);
			dependencyMaxChars.value = String(
				dependencyStrategy?.maxChars ?? 24000
			);
			dependencyIncludeInferred.checked =
				dependencyStrategy
					?.includeInferred ?? false;

			updateDependencyFieldsVisibility();

			dialog.showModal();
			instructionsInput.focus();
		}

		function createRule() {
			const id =
				'rule-' + Date.now().toString(36)
				+ '-' + Math.random().toString(36).slice(2, 8);

			config.rules.push({
				id,
				name: 'New summarization rule',
				glob: '**/*',
				priority: 100,
				enabled: true,
				instructions:
					'Describe what matters when summarizing matching files.',
			});

			selectedKey = id;
			render();
			openEditor(id);
		}

		function saveEditor() {
			const instructions = instructionsInput.value.trim();

			if (!instructions) {
				setStatus(
					'Instructions cannot be empty.',
					'error'
				);
				return;
			}

			if (editingKey === 'general') {
				config.generalInstructions = instructions;
			} else {
				const rule = findRule(editingKey);
				if (!rule) {
					return;
				}

				const name = nameInput.value.trim();
				const glob = globInput.value.trim();
				const priority = Number(priorityInput.value);

				if (!name || !glob || !Number.isFinite(priority)) {
					setStatus(
						'Name, glob, and numeric priority are required.',
						'error'
					);
					return;
				}

				rule.name = name;
				rule.glob = glob;
				rule.priority = priority;
				rule.enabled = enabledInput.checked;
				rule.instructions = instructions;

				if (dependencyEnabled.checked) {
					const follow = [
						...new Set(
							dependencyKinds.value
								.split(',')
								.map((kind) => kind.trim())
								.filter((kind) => kind.length > 0)
						),
					];
					const maxFiles = Number(
						dependencyMaxFiles.value
					);
					const maxChars = Number(
						dependencyMaxChars.value
					);

					if (
						follow.length === 0
						|| !Number.isInteger(maxFiles)
						|| maxFiles < 1
						|| !Number.isInteger(maxChars)
						|| maxChars < 1
					) {
						setStatus(
							'Dependency kinds, maximum files, and maximum characters must be valid.',
							'error'
						);
						return;
					}

					rule.dependencyStrategy = {
						follow,
						maxDepth: 1,
						maxFiles,
						maxChars,
						includeInferred:
							dependencyIncludeInferred.checked,
					};
				} else {
					delete rule.dependencyStrategy;
				}
			}

			saveConfig();
		}

		function deleteEditorRule() {
			if (!editingKey || editingKey === 'general') {
				return;
			}

			config.rules = config.rules.filter(
				(rule) => rule.id !== editingKey
			);
			selectedKey = undefined;
			editButton.disabled = true;
			dialog.close();
			saveConfig();
		}

		function saveConfig() {
			if (saveInFlight) {
				return;
			}

			saveInFlight = true;
			setStatus('Saving...', 'info');
			saveButton.disabled = true;
			deleteButton.disabled = true;

			vscode.postMessage({
				type: 'save',
				config,
			});
		}

		addButton.addEventListener('click', createRule);

		editButton.addEventListener('click', () => {
			if (selectedKey) {
				openEditor(selectedKey);
			}
		});

		cancelButton.addEventListener('click', () => {
			dialog.close();
		});

		saveButton.addEventListener('click', saveEditor);
		dependencyEnabled.addEventListener(
			'change',
			updateDependencyFieldsVisibility
		);
		deleteButton.addEventListener(
			'click',
			deleteEditorRule
		);

		function clearPatternTestDisplay() {
			if (patternTestTimer) {
				clearTimeout(patternTestTimer);
				patternTestTimer = undefined;
			}

			patternTestSummary.textContent = '';
			patternTestSummary.className =
				'pattern-test-summary';
			patternTestSummary.hidden = true;
			togglePatternMatches.hidden = true;
			patternTestMatches.hidden = true;
			patternTestMatches.textContent = '';
		}

		function schedulePatternTest(immediate) {
			if (!testPatternEnabled.checked) {
				clearPatternTestDisplay();
				return;
			}

			if (patternTestTimer) {
				clearTimeout(patternTestTimer);
			}

			const run = () => {
				const glob = globInput.value.trim();
				const requestId = ++patternTestRequestId;

				patternTestSummary.hidden = false;
				patternTestSummary.className =
					'pattern-test-summary';
				patternTestSummary.textContent = 'Testing...';
				togglePatternMatches.hidden = true;
				patternTestMatches.hidden = true;

				vscode.postMessage({
					type: 'testPattern',
					glob,
					requestId,
				});
			};

			if (immediate) {
				run();
				return;
			}

			patternTestTimer = setTimeout(run, 350);
		}

		testPatternEnabled.addEventListener('change', () => {
			if (!testPatternEnabled.checked) {
				clearPatternTestDisplay();
				return;
			}

			schedulePatternTest(true);
		});

		globInput.addEventListener('input', () => {
			schedulePatternTest(false);
		});

		togglePatternMatches.addEventListener('click', () => {
			patternMatchesExpanded = !patternMatchesExpanded;
			patternTestMatches.hidden =
				!patternMatchesExpanded;
			togglePatternMatches.textContent =
				patternMatchesExpanded
					? 'Hide matching files ▾'
					: 'Show matching files ▸';
		});

		window.addEventListener('message', (event) => {
			const message = event.data;

			if (!message || typeof message.type !== 'string') {
				return;
			}

			if (message.type === 'saveSuccess') {
				config = message.config;
				saveInFlight = false;
				saveButton.disabled = false;
				deleteButton.disabled = false;
				dialog.close();
				render();
				setStatus('Configuration saved.', 'success');
				return;
			}

			if (message.type === 'saveError') {
				saveInFlight = false;
				saveButton.disabled = false;
				deleteButton.disabled = false;
				setStatus(
					message.error || 'Failed to save.',
					'error'
				);
				return;
			}

			if (
				message.type === 'patternTestResult'
				|| message.type === 'patternTestError'
			) {
				if (
					message.requestId !== patternTestRequestId
				) {
					return;
				}

				patternTestSummary.hidden = false;

				if (message.type === 'patternTestError') {
					patternTestSummary.textContent =
						message.error || 'Pattern test failed.';
					patternTestSummary.className =
						'pattern-test-summary error';
					togglePatternMatches.hidden = true;
					patternTestMatches.hidden = true;
					return;
				}

				const paths = Array.isArray(message.previewPaths)
					? message.previewPaths
					: [];

				patternTestSummary.textContent =
					message.totalMatches + ' matches';
				patternTestSummary.className =
					'pattern-test-summary';

				patternTestMatches.textContent = '';

				if (paths.length === 0) {
					togglePatternMatches.hidden = true;
					patternTestMatches.hidden = true;
					return;
				}

				const list = document.createElement('ul');

				for (const filePath of paths) {
					const item = document.createElement('li');
					item.textContent = filePath;
					list.appendChild(item);
				}

				patternTestMatches.appendChild(list);

				if (message.omittedCount > 0) {
					const omitted =
						document.createElement('div');
					omitted.className = 'omitted';
					omitted.textContent =
						message.omittedCount
						+ ' additional files omitted';
					patternTestMatches.appendChild(omitted);
				}

				togglePatternMatches.hidden = false;
				togglePatternMatches.textContent =
					patternMatchesExpanded
						? 'Hide matching files ▾'
						: 'Show matching files ▸';
				patternTestMatches.hidden =
					!patternMatchesExpanded;
			}

		});

		render();
	</script>
</body>
</html>`;
}

export class SummarizationConfigPanel
implements vscode.Disposable {
	private panel: vscode.WebviewPanel | undefined;

	constructor(
		private readonly options: SummarizationConfigPanelOptions
	) {}

	async show(): Promise<void> {
		const config = await this.options.load();

		if (this.panel) {
			this.panel.webview.html =
				buildSummarizationConfigHtml(config);
			this.panel.reveal(vscode.ViewColumn.Active);
			return;
		}

		this.panel = vscode.window.createWebviewPanel(
			'aiDev.summarizationConfig',
			'Summarization Configuration',
			vscode.ViewColumn.Active,
			{
				enableScripts: true,
				retainContextWhenHidden: true,
			}
		);

		this.panel.webview.html =
			buildSummarizationConfigHtml(config);

		this.panel.webview.onDidReceiveMessage(
			async (message: {
				type?: string;
				config?: unknown;
				glob?: unknown;
				requestId?: unknown;
			}) => {
				if (message.type === 'testPattern') {
					const requestId =
						typeof message.requestId === 'number'
							? message.requestId
							: 0;

					try {
						if (typeof message.glob !== 'string') {
							throw new Error(
								'A glob pattern is required.'
							);
						}

						const glob = message.glob.trim();
						const syntaxIssue =
							validateSummarizationGlobSyntax(glob);

						if (syntaxIssue) {
							throw new Error(syntaxIssue);
						}

						const result =
							await this.options.testPattern(glob);

						await this.panel?.webview.postMessage({
							type: 'patternTestResult',
							requestId,
							...result,
						});
					} catch (error) {
						await this.panel?.webview.postMessage({
							type: 'patternTestError',
							requestId,
							error:
								error instanceof Error
									? error.message
									: String(error),
						});
					}

					return;
				}

				if (
					message.type !== 'save'
					|| !message.config
				) {
					return;
				}

				try {
					const candidate =
						message.config as SummarizationConfig;
					const issues =
						validateSummarizationConfig(candidate);

					if (issues.length > 0) {
						throw new Error(
							issues
								.map((issue) => issue.message)
								.join(' ')
						);
					}

					await this.options.save(candidate);

					await this.panel?.webview.postMessage({
						type: 'saveSuccess',
						config: candidate,
					});
				} catch (error) {
					await this.panel?.webview.postMessage({
						type: 'saveError',
						error:
							error instanceof Error
								? error.message
								: String(error),
					});
				}
			}
		);

		this.panel.onDidDispose(() => {
			this.panel = undefined;
		});
	}

	dispose(): void {
		this.panel?.dispose();
		this.panel = undefined;
	}
}

export function createEmptySummarizationConfigForPanel():
SummarizationConfig {
	return createDefaultSummarizationConfig();
}

export const escapeSummarizationConfigHtml = escapeHtml;
