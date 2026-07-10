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
});
