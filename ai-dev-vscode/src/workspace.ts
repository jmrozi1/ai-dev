import * as path from 'node:path';
import * as vscode from 'vscode';

export interface ActiveWorkspaceContext {
	activeFileUri: vscode.Uri;
	workspaceRoot: string;
}

interface ExpectedDocumentationPathOptions {
	workspaceRoot: string;
	sourceFilePath: string;
	docsDir?: string;
}

const DEFAULT_DOCS_DIR = 'ai-docs';

export function normalizePathForMarkdown(filePath: string): string {
	return filePath
		.replace(/\\/g, '/')
		.split(path.sep)
		.join('/');
}

export function getSelectedSourcePath(workspaceRoot: string, activeFilePath: string): string {
	return normalizePathForMarkdown(path.relative(workspaceRoot, activeFilePath));
}

function normalizeDocsDir(rawDocsDir: string | undefined): string {
	const normalized = normalizePathForMarkdown((rawDocsDir ?? DEFAULT_DOCS_DIR).trim());
	const withoutTrailingSlash = normalized.replace(/\/+$/, '');
	return withoutTrailingSlash.length > 0 ? withoutTrailingSlash : DEFAULT_DOCS_DIR;
}

function ensureSafeRelativeDocumentationPath(expectedPath: string): string {
	const normalized = normalizePathForMarkdown(expectedPath).replace(/^\.\//, '');
	const collapsed = path.posix.normalize(normalized);

	if (
		collapsed.length === 0
		|| collapsed === '.'
		|| path.posix.isAbsolute(collapsed)
		|| collapsed === '..'
		|| collapsed.startsWith('../')
	) {
		throw new Error('Unsafe documentation summary path resolved.');
	}

	return collapsed;
}

export function getExpectedDocumentationSummaryPath(options: ExpectedDocumentationPathOptions): string {
	const sourcePath = getSelectedSourcePath(options.workspaceRoot, options.sourceFilePath);
	const sourceParentDirectory = normalizePathForMarkdown(path.posix.dirname(sourcePath));
	const docsDir = normalizeDocsDir(options.docsDir);
	const expectedPath = sourceParentDirectory === '.' || sourceParentDirectory.length === 0
		? path.posix.join(docsDir, 'summary.md')
		: path.posix.join(docsDir, sourceParentDirectory, 'summary.md');

	return ensureSafeRelativeDocumentationPath(expectedPath);
}

export function getExpectedDirectorySummaryPath(options: {
	workspaceRoot: string;
	sourceFilePath: string;
	docsDir?: string;
}): string {
	return getExpectedDocumentationSummaryPath(options);
}

export function getActiveWorkspaceContext(): ActiveWorkspaceContext | undefined {
	const activeEditor = vscode.window.activeTextEditor;
	if (!activeEditor) {
		return undefined;
	}

	const activeFileUri = activeEditor.document.uri;
	const workspaceFolder = vscode.workspace.getWorkspaceFolder(activeFileUri);
	if (!workspaceFolder) {
		return undefined;
	}

	return {
		activeFileUri,
		workspaceRoot: workspaceFolder.uri.fsPath,
	};
}

export function getOpenWorkspaceRoot(): string | undefined {
	return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}