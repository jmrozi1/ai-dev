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

export interface DirectPromptDependencyContextFile {
	primarySourcePath: string;
	path: string;
	relationshipKind: string;
	resolution: 'exact' | 'inferred';
	evidence: string[];
	contents: string;
}

export function appendDependencyContextToDirectPromptMarkdown(
	prompt: string,
	dependencyContextFiles: DirectPromptDependencyContextFile[]
): string {
	if (dependencyContextFiles.length === 0) {
		return prompt;
	}

	const marker = '\nInstructions:\n';
	const markerIndex = prompt.lastIndexOf(marker);

	if (markerIndex < 0) {
		throw new Error(
			'Direct summarization prompt is missing its Instructions section.'
		);
	}

	const dependencyLines = [
		'',
		'Resolved dependency context:',
		'Use this context only to explain behavior delegated by the primary source file.',
	];

	for (const dependencyFile of dependencyContextFiles) {
		dependencyLines.push(
			'',
			`Primary source: ${dependencyFile.primarySourcePath}`,
			`Dependency file: ${dependencyFile.path}`,
			`Relationship: ${dependencyFile.relationshipKind}`,
			`Resolution: ${dependencyFile.resolution}`,
			'Evidence:',
			...dependencyFile.evidence.map(
				(evidence) => `- ${evidence}`
			),
			'',
			'```text',
			dependencyFile.contents,
			'```'
		);
	}

	dependencyLines.push('');

	return [
		prompt.slice(0, markerIndex),
		dependencyLines.join('\n'),
		prompt.slice(markerIndex),
	].join('');
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
	dependencyContextFiles?: Array<{
		primarySourcePath: string;
		path: string;
		relationshipKind: string;
		resolution: 'exact' | 'inferred';
		evidence: string[];
		contents: string;
	}>;
	sourceSetIsAuthoritative?: boolean;
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
		dependencyContextFiles = [],
		sourceSetIsAuthoritative = false,
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

	if (dependencyContextFiles.length > 0) {
		lines.push(
			'',
			'Resolved dependency context:',
			'Use this context only to explain behavior delegated by the primary source file.'
		);

		for (
			const dependencyFile
			of dependencyContextFiles
		) {
			lines.push(
				'',
				`Primary source: ${dependencyFile.primarySourcePath}`,
				`Dependency file: ${dependencyFile.path}`,
				`Relationship: ${dependencyFile.relationshipKind}`,
				`Resolution: ${dependencyFile.resolution}`,
				'Evidence:',
				...dependencyFile.evidence.map(
					(evidence) => `- ${evidence}`
				),
				'',
				'```text',
				dependencyFile.contents,
				'```'
			);
		}
	}

	lines.push(
		'',
		'Instructions:',
		'Generate the complete updated markdown for the target summary file.',
		'Update or add entries for all selected source files in that summary file.',
		sourceSetIsAuthoritative
			? 'The selected source files are the complete authoritative current source set for this target summary. Remove entries for source files not in this set.'
			: 'Preserve useful existing entries for source files not in the selected set unless clearly stale or incorrect.',
		'Do not generate standalone per-source documentation files.',
		'Treat provided primary source file contents as final authority and follow the workflow and template.',
		'Use dependency context only when needed to explain delegated behavior; do not turn the entry into a dependency-tree summary.',
		'Return the complete updated contents of the target summary.md file.',
		'Return raw markdown only.',
		'Do not wrap the response in ```markdown or any other code fence.',
		'Do not include explanatory text before or after the file contents.'
	);

	return lines.join('\n');
}

export function buildGenerateArchitectureSummaryDirectPromptMarkdown(params: {
	workspaceRoot: string;
	workflowFilePath: string;
	workflowFileContents: string;
	templateFilePath: string;
	templateFileContents: string;
	aiDevYamlLabel: string;
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
		aiDevYamlLabel,
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
		aiDevYamlLabel,
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
		'Use .ai-dev.yaml to find the documentation directory, then start from ai-docs/architecture-summary.md when available. Use ai-docs/summary.md only as a legacy fallback.',
		'Use summary files to route to relevant docs and source.',
		'Do not read every source file by default.',
	].join('\n');
}

export function buildAnswerFromAiDocsDirectPromptMarkdown(params: {
	workspaceRoot: string;
	workflowFilePath: string;
	workflowFileContents: string;
	aiDevYamlLabel: string;
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
	verifiedSourceFiles?: Array<{
		path: string;
		role: 'primary-source' | 'dependency';
		primarySourcePath?: string;
		relationshipKind?: string;
		resolution?: 'exact' | 'inferred';
		evidence?: string[];
		contents: string;
	}>;
	userQuestion: string;
}): string {
	const {
		workspaceRoot,
		workflowFilePath,
		workflowFileContents,
		aiDevYamlLabel,
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
		verifiedSourceFiles,
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
		aiDevYamlLabel,
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
		rootSummaryExists && !rootSummaryEmpty
			? '- Limitation: none; primary root architecture/routing context was available.'
			: '- Limitation: fallback discovered summaries provide less complete routing than a fully maintained root architecture summary.',
		'',
		rootSummaryExists ? 'Root architecture/routing context:' : 'Root architecture/routing context: not available',
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

	if (verifiedSourceFiles && verifiedSourceFiles.length > 0) {
		lines.push(
			'',
			'Verified authoritative source context:'
		);

		for (const file of verifiedSourceFiles) {
			lines.push(
				'',
				`Verified source: ${file.path}`,
				`Role: ${file.role}`,
			);

			if (file.primarySourcePath) {
				lines.push(
					`Primary source: ${file.primarySourcePath}`
				);
			}

			if (file.relationshipKind) {
				lines.push(
					`Relationship: ${file.relationshipKind}`
				);
			}

			if (file.resolution) {
				lines.push(
					`Resolution: ${file.resolution}`
				);
			}

			if (file.evidence?.length) {
				lines.push(
					'Evidence:',
					...file.evidence.map(
						(item) => `- ${item}`
					)
				);
			}

			lines.push(
				'',
				'```text',
				file.contents,
				'```'
			);
		}
	}

	lines.push(
		'',
		'Instructions:',
		'Answer the user question using only the provided context.',
		'Treat the root architecture/routing context as preferred routing, and follow linked child summaries before concluding coverage.',
		'Treat fallback discovered summaries as gapped routing context, not full routing coverage.',
		'If a root architecture/routing context was provided, do not warn that ai-docs/summary.md is missing.',
		'If your answer relies only on fallback discovered summaries, state that the root architecture/routing context was missing or incomplete for this question.',
		'Cite the summary file(s) you used.',
		'Use verified authoritative source context to confirm exact behavior, values, mechanics, and delegated implementation.',
		'When summaries and verified source disagree, treat verified source as authoritative and mention the discrepancy.',
		'Do not claim source verification for facts supported only by summaries.',
		'If provided routing context is still insufficient, clearly list the additional files you need.'
	);

	return lines.join('\n');
}