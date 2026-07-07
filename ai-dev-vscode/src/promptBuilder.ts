import { normalizePathForMarkdown } from './workspace';

function toDisplayPath(filePath: string): string {
	return normalizePathForMarkdown(filePath).replace(/\/+$/, '');
}

function joinDisplayPath(basePath: string, suffix: string): string {
	const normalizedBase = toDisplayPath(basePath);
	const normalizedSuffix = suffix.replace(/^\/+/, '');
	return `${normalizedBase}/${normalizedSuffix}`;
}

export function buildUnitDocPromptMarkdown(params: {
	workspaceRoot: string;
	aiDevCorePath: string;
	selectedSourcePath: string;
	targetSummaryPath: string;
}): string {
	const {
		workspaceRoot,
		aiDevCorePath,
		selectedSourcePath,
		targetSummaryPath,
	} = params;

	return [
		'AI Dev task: generate-unit-doc',
		'',
		'Workflow:',
		joinDisplayPath(aiDevCorePath, 'workflows/generate-docs/generate-unit-doc.md'),
		'',
		'Template:',
		joinDisplayPath(aiDevCorePath, 'workflows/generate-docs/templates/unit-doc.md'),
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'Input:',
		`Source file: ${selectedSourcePath}`,
		`Target summary file: ${targetSummaryPath}`,
		'',
		'Instructions:',
		'Read the workflow and template files, then create or update the target summary file.',
		'Update or add the selected source file entry inside the target summary file.',
		'Do not create a standalone per-source documentation file.',
		'Treat the source file as final authority.',
		'Do not invent behavior.',
		'Return the complete updated contents of the target summary.md file.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.',
		'',
	].join('\n');
}

export function buildGroupedGenerateUnitDocDirectPromptMarkdown(params: {
	workspaceRoot: string;
	workflowFilePath: string;
	workflowFileContents: string;
	templateFilePath: string;
	templateFileContents: string;
	targetSummaryPath: string;
	existingSummaryContents?: string;
	selectedSourceFiles: Array<{
		path: string;
		contents: string;
	}>;
}): string {
	const {
		workspaceRoot,
		workflowFilePath,
		workflowFileContents,
		templateFilePath,
		templateFileContents,
		targetSummaryPath,
		existingSummaryContents,
		selectedSourceFiles,
	} = params;

	const lines = [
		'AI Dev direct task: generate-unit-doc-batch-grouped',
		'',
		`Workspace: ${toDisplayPath(workspaceRoot)}`,
		`Target summary file path: ${targetSummaryPath}`,
		'',
		'Workflow file:',
		workflowFilePath,
		'',
		'```markdown',
		workflowFileContents,
		'```',
		'',
		'Template file:',
		templateFilePath,
		'',
		'```markdown',
		templateFileContents,
		'```',
		'',
		existingSummaryContents !== undefined ? 'Existing target summary contents:' : 'Existing target summary contents: not found',
	];

	if (existingSummaryContents !== undefined) {
		lines.push(targetSummaryPath, '', '```markdown', existingSummaryContents, '```', '');
	} else {
		lines.push('');
	}

	lines.push('Selected source files:');
	if (selectedSourceFiles.length > 0) {
		lines.push(...selectedSourceFiles.map((item) => `- ${item.path}`));
	} else {
		lines.push('- none');
	}

	for (const sourceFile of selectedSourceFiles) {
		lines.push('', 'Source file contents:', sourceFile.path, '', '```text', sourceFile.contents, '```');
	}

	lines.push(
		'',
		'Instructions:',
		'Generate the complete updated markdown for the target summary file.',
		'Update or add entries for all selected source files in that summary file.',
		'Preserve useful existing entries for source files not in the selected set unless clearly stale or incorrect.',
		'Do not generate standalone per-source documentation files.',
		'Treat provided source file contents as final authority and follow the workflow and template.',
		'Return the complete updated contents of the target summary.md file.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.'
	);

	return lines.join('\n');
}

export function buildGenerateArchitectureSummaryPromptMarkdown(params: {
	workspaceRoot: string;
	aiDevCorePath: string;
	docsDir: string;
	targetArchitecturePath: string;
	selectedDirectories: Array<{
		sourceDirectory: string;
		summaryPath: string;
		summaryStatus: 'exists' | 'missing' | 'empty';
	}>;
	omittedDirectories?: Array<{
		sourceDirectory: string;
		summaryPath: string;
		summaryStatus: 'exists' | 'missing' | 'empty';
	}>;
}): string {
	const {
		workspaceRoot,
		aiDevCorePath,
		docsDir,
		targetArchitecturePath,
		selectedDirectories,
		omittedDirectories,
	} = params;

	const lines = [
		'AI Dev task: generate-architecture-summary',
		'',
		'Workflow:',
		joinDisplayPath(aiDevCorePath, 'workflows/generate-docs/generate-architecture-summary.md'),
		'',
		'Template:',
		joinDisplayPath(aiDevCorePath, 'workflows/generate-docs/templates/architecture-summary.md'),
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'Input:',
		`docsDir: ${docsDir}`,
		`Target architecture summary file: ${targetArchitecturePath}`,
		'',
		'Selected source directories and expected summary files:',
	];

	if (selectedDirectories.length > 0) {
		lines.push(
			...selectedDirectories.map(
				(item) => `- Source directory: ${item.sourceDirectory} | Summary: ${item.summaryPath} | Status: ${item.summaryStatus}`
			)
		);
	} else {
		lines.push('- none');
	}

	if (omittedDirectories && omittedDirectories.length > 0) {
		lines.push(
			'',
			'Omitted source directories (not selected):',
			...omittedDirectories.map(
				(item) => `- Source directory: ${item.sourceDirectory} | Summary: ${item.summaryPath} | Status: ${item.summaryStatus}`
			)
		);
	}

	lines.push(
		'',
		'Instructions:',
		'Read the workflow and template files, then create or update the target architecture summary file.',
		'Architecture summary must stay directory-level only and route to directory summary files.',
		'Do not list source files.',
		'Do not identify key files.',
		'Treat missing summary files as known routing gaps.',
		'Do not generate directory summary files in this task.',
		'Return the complete updated contents of architecture-summary.md.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.',
		''
	);

	return lines.join('\n');
}

export function buildGenerateArchitectureSummaryDirectPromptMarkdown(params: {
	workspaceRoot: string;
	workflowFilePath: string;
	workflowFileContents: string;
	templateFilePath: string;
	templateFileContents: string;
	aiDevYamlPath: string;
	aiDevYamlContents: string;
	docsDir: string;
	targetArchitecturePath: string;
	existingArchitectureSummaryContents?: string;
	selectedDirectories: Array<{
		sourceDirectory: string;
		summaryPath: string;
		summaryStatus: 'exists' | 'missing' | 'empty';
		summaryContents?: string;
	}>;
	omittedDirectories?: Array<{
		sourceDirectory: string;
		summaryPath: string;
		summaryStatus: 'exists' | 'missing' | 'empty';
	}>;
}): string {
	const {
		workspaceRoot,
		workflowFilePath,
		workflowFileContents,
		templateFilePath,
		templateFileContents,
		aiDevYamlPath,
		aiDevYamlContents,
		docsDir,
		targetArchitecturePath,
		existingArchitectureSummaryContents,
		selectedDirectories,
		omittedDirectories,
	} = params;

	const lines = [
		'AI Dev direct task: generate-architecture-summary',
		'',
		`Workspace: ${toDisplayPath(workspaceRoot)}`,
		`docsDir: ${docsDir}`,
		`Target architecture summary path: ${targetArchitecturePath}`,
		'',
		'Workflow file:',
		workflowFilePath,
		'',
		'```markdown',
		workflowFileContents,
		'```',
		'',
		'Template file:',
		templateFilePath,
		'',
		'```markdown',
		templateFileContents,
		'```',
		'',
		'.ai-dev.yaml file:',
		aiDevYamlPath,
		'',
		'```yaml',
		aiDevYamlContents,
		'```',
		'',
		existingArchitectureSummaryContents !== undefined
			? 'Existing architecture summary contents:'
			: 'Existing architecture summary contents: not found',
	];

	if (existingArchitectureSummaryContents !== undefined) {
		lines.push(targetArchitecturePath, '', '```markdown', existingArchitectureSummaryContents, '```', '');
	} else {
		lines.push('');
	}

	lines.push('Selected source directories and expected summary files:');
	if (selectedDirectories.length > 0) {
		lines.push(
			...selectedDirectories.map(
				(item) => `- Source directory: ${item.sourceDirectory} | Summary: ${item.summaryPath} | Status: ${item.summaryStatus}`
			)
		);
	} else {
		lines.push('- none');
	}

	for (const item of selectedDirectories) {
		if (item.summaryContents === undefined) {
			continue;
		}

		lines.push(
			'',
			`Summary contents for ${item.summaryPath}:`,
			'',
			'```markdown',
			item.summaryContents,
			'```'
		);
	}

	if (omittedDirectories && omittedDirectories.length > 0) {
		lines.push(
			'',
			'Omitted source directories (not selected):',
			...omittedDirectories.map(
				(item) => `- Source directory: ${item.sourceDirectory} | Summary: ${item.summaryPath} | Status: ${item.summaryStatus}`
			)
		);
	}

	lines.push(
		'',
		'Hard rules:',
		'- Architecture summary is directory-level only.',
		'- Route to directory summary files, not source files.',
		'- Do not list source files.',
		'- Do not identify key files.',
		'- Missing summaries are known gaps and should be represented as such.',
		'- Do not use or request source file contents for this task.',
		'',
		'Instructions:',
		'Generate the complete updated markdown for architecture-summary.md.',
		'Preserve useful existing directory routing entries unless they are stale or incorrect.',
		'Follow the workflow and template exactly.',
		'Return the complete updated contents of architecture-summary.md.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.'
	);

	return lines.join('\n');
}

export function buildReviewDocumentationPromptMarkdown(params: {
	aiDevCorePath: string;
	workspaceRoot: string;
	changedFilePaths: string[];
	deterministicFindingsMarkdown?: string;
}): string {
	const { aiDevCorePath, workspaceRoot, changedFilePaths, deterministicFindingsMarkdown } = params;
	const changedFilesLines = changedFilePaths.map((filePath) => `- ${filePath}`);

	return [
		'AI Dev task: review-changed-docs',
		'',
		'Workflow:',
		joinDisplayPath(aiDevCorePath, 'workflows/review/review-documentation.md'),
		'',
		'Finding template:',
		joinDisplayPath(aiDevCorePath, 'workflows/review/finding-template.md'),
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'Input:',
		'Changed files:',
		'',
		...changedFilesLines,
		'',
		...(deterministicFindingsMarkdown
			? [deterministicFindingsMarkdown, '']
			: []),
		'Instructions:',
		'Read the workflow and finding template.',
		'Use deterministic mapping findings as mandatory findings to include alongside model analysis.',
		'Review changed files and related AI documentation for stale docs, missing docs, poor routing, and risky documentation drift.',
		'Report findings only unless explicitly asked to edit files.',
	].join('\n');
}

export function buildFileDocumentationReviewPromptMarkdown(params: {
	aiDevCorePath: string;
	workspaceRoot: string;
	selectedSourcePath: string;
	targetSummaryPath: string;
	summaryFile: string;
}): string {
	const {
		aiDevCorePath,
		workspaceRoot,
		selectedSourcePath,
		targetSummaryPath,
		summaryFile,
	} = params;

	return [
		'AI Dev task: review-file-docs',
		'',
		'Workflow:',
		joinDisplayPath(aiDevCorePath, 'workflows/review/review-documentation.md'),
		'',
		'Finding template:',
		joinDisplayPath(aiDevCorePath, 'workflows/review/finding-template.md'),
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'Input:',
		`Source file: ${selectedSourcePath}`,
		`Target summary file: ${targetSummaryPath}`,
		'',
		`Summary file: ${summaryFile}`,
		'',
		'Instructions:',
		'Read the workflow and finding template.',
		'Review the source file against its expected directory summary file and documentation summary routing artifact.',
		'Report findings only unless explicitly asked to edit files.',
		'',
	].join('\n');
}

export function buildAnswerPromptMarkdown(params: {
	workspaceRoot: string;
	aiDevCorePath: string;
	userQuestion: string;
}): string {
	const { workspaceRoot, aiDevCorePath, userQuestion } = params;

	return [
		'AI Dev task: answer-from-ai-docs',
		'',
		'Workflow:',
		joinDisplayPath(aiDevCorePath, 'workflows/answer-docs/answer-from-ai-docs.md'),
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'Input:',
		`Question: ${userQuestion}`,
		'',
		'Instructions:',
		'Read and follow the workflow file.',
		'Use .ai-dev.yaml to find the documentation summary file, then start from ai-docs/summary.md when available.',
		'Use summary files to route to relevant docs and source.',
		'Do not read every source file by default.',
	].join('\n');
}

export function buildAnswerFromAiDocsDirectPromptMarkdown(params: {
	workspaceRoot: string;
	workflowFilePath: string;
	workflowFileContents: string;
	aiDevYamlPath: string;
	aiDevYamlContents: string;
	rootSummaryPath: string;
	rootSummaryExists: boolean;
	rootSummaryEmpty: boolean;
	rootSummaryContents?: string;
	docsDir: string;
	discoveredSummaryCount: number;
	fallbackIncludedReason?: string;
	routedDocumentationFiles?: Array<{
		path: string;
		kind: 'summary' | 'dependency-map' | 'routing-artifact';
		contents: string;
	}>;
	fallbackDiscoveredSummaries?: Array<{
		path: string;
		contents: string;
		score: number;
	}>;
	missingDocumentationPaths?: string[];
	userQuestion: string;
}): string {
	const {
		workspaceRoot,
		workflowFilePath,
		workflowFileContents,
		aiDevYamlPath,
		aiDevYamlContents,
		rootSummaryPath,
		rootSummaryExists,
		rootSummaryEmpty,
		rootSummaryContents,
		docsDir,
		discoveredSummaryCount,
		fallbackIncludedReason,
		routedDocumentationFiles,
		fallbackDiscoveredSummaries,
		missingDocumentationPaths,
		userQuestion,
	} = params;

	const lines = [
		'AI Dev direct task: answer-from-ai-docs',
		'',
		'Context:',
		'You only have access to the provided context for this first direct-mode attempt.',
		'If the documentation summary is insufficient, say which additional files are needed.',
		'',
		'Workspace:',
		toDisplayPath(workspaceRoot),
		'',
		'User question:',
		userQuestion,
		'',
		'Workflow file:',
		workflowFilePath,
		'',
		'```markdown',
		workflowFileContents,
		'```',
		'',
		'.ai-dev.yaml file:',
		aiDevYamlPath,
		'',
		'```yaml',
		aiDevYamlContents,
		'```',
		'',
		'Summary routing metadata:',
		`- Configured root summary path: ${rootSummaryPath}`,
		`- Root summary exists: ${rootSummaryExists}`,
		`- Root summary empty: ${rootSummaryEmpty}`,
		`- docsDir searched for fallback summaries: ${docsDir}`,
		`- Discovered fallback summary files: ${discoveredSummaryCount}`,
		`- Fallback inclusion reason: ${fallbackIncludedReason ?? 'none (root routing was sufficient)'}`,
		'- Limitation: fallback discovered summaries provide less complete routing than a fully maintained root summary.',
		'',
		rootSummaryExists ? 'Root summary:' : 'Root summary: not available',
	];

	if (rootSummaryExists) {
		lines.push(rootSummaryPath);
	}

	if (rootSummaryContents) {
		lines.push('', '```markdown', rootSummaryContents, '```');
	}

	if (routedDocumentationFiles && routedDocumentationFiles.length > 0) {
		lines.push('', 'Routed documentation context (from root summary links):');
		for (const file of routedDocumentationFiles) {
			const kindLabel = file.kind === 'summary'
				? 'Routed summary'
				: file.kind === 'dependency-map'
					? 'Dependency map'
					: 'Routing artifact';
			lines.push('', `${kindLabel}:`, file.path, '', '```markdown', file.contents, '```');
		}
	}

	if (fallbackDiscoveredSummaries && fallbackDiscoveredSummaries.length > 0) {
		lines.push('', 'Fallback discovered summaries (scored against the user question):');
		for (const file of fallbackDiscoveredSummaries) {
			lines.push('', `Fallback summary (score ${file.score}):`, file.path, '', '```markdown', file.contents, '```');
		}
	}

	if (missingDocumentationPaths && missingDocumentationPaths.length > 0) {
		lines.push(
			'',
			'Linked documentation files that were referenced but unavailable:',
			...missingDocumentationPaths.map((filePath) => `- ${filePath}`)
		);
	}

	lines.push(
		'',
		'Instructions:',
		'Answer the user question using only the provided context.',
		'Treat the root summary as preferred routing, and follow linked child summaries before concluding coverage.',
		'Treat fallback discovered summaries as gapped routing context, not full routing coverage.',
		'If your answer relies on fallback discovered summaries, state that the root summary was missing or incomplete for this question.',
		'Cite the summary file(s) you used.',
		'Verify exact behavior against source files before asserting specific runtime values or mechanics.',
		'If provided routing context is still insufficient, clearly list the additional files you need.'
	);

	return lines.join('\n');
}