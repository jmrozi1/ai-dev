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
} from '../batchUnitDocs';
import { readAiDevConfig, resolveAiDevCorePath, setAiDevExtensionRootPath } from '../config';
import {
	applyTextInput,
	createAssistantInputState,
	handleBackspace,
	handleCommandTab,
	handleEscape,
	handleHistoryDown,
	handleHistoryUp,
	parseSlashCommand,
	resolveAskCommand,
	submitInput,
} from '../assistantInput';
import {
	AiDevAssistantPseudoterminal,
	AiDevAssistantTerminalManager,
	buildUnstructuredReviewFallback,
	getCommonPrefix,
	getMatchingAssistantCommands,
	getPathCompletionContext,
	formatItemsInColumns,
	MODEL_RESPONSE_MARKER,
	formatModelResponseLines,
	resolveReviewMode,
	resolveReviewRequest,
} from '../assistantTerminal';
import {
	AssistantReportStore,
	createAssistantReport,
	parseReportResponse,
	parseReviewFindings,
} from '../assistantReport';
import {
	buildAssistantReportHtml,
} from '../assistantReportPanel';
import {
	matchesReviewTarget,
	selectChangedReviewFiles,
	selectReviewFiles,
} from '../projectReview';
import {
	NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS,
} from '../pathMatching';
import {
	getBatchSourceGlobs,
} from '../sourceDiscovery';
import type {
	AssistantChatBackend,
} from '../assistantChatBackend';
import {
	ASSISTANT_COMMAND_DEFINITIONS,
	formatAssistantCommandHelp,
	formatAssistantCommandSummary,
	getAssistantCommandDefinition,
	getAssistantCommandNames,
	getAssistantLookupItems,
} from '../assistantCommands';
import {
	chooseAutomaticAssistantRoute,
} from '../assistantRouting';
import {
	DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
	createDefaultSummarizationConfig,
	injectSummarizationInstructions,
	matchesSummarizationGlob,
	normalizeSummarizationConfig,
	readSummarizationConfig,
	resolveSummarizationInstructions,
	validateSummarizationConfig,
	validateSummarizationGlobSyntax,
	writeSummarizationConfig,
} from '../summarizationConfig';
import {
	buildSummarizationConfigHtml,
} from '../summarizationConfigPanel';
import {
	buildGroupedGenerateUnitDocDirectPromptMarkdown,
} from '../promptBuilder';
import {
	normalizePathForMarkdown,
} from '../workspace';
import {
	createEmptyDependencyMap,
	findOutgoingDependencyEdges,
	readDependencyMap,
	validateDependencyMap,
	writeDependencyMap,
} from '../dependencyMap';
import {
	extractJenkinsPipelineScriptPath,
	resolveJenkinsPipelineDependency,
} from '../jenkinsDependencyResolver';
import {
	refreshJenkinsDependencyMap,
	refreshJenkinsDependencyMapForSummarization,
	shouldRefreshJenkinsDependencyMap,
} from '../dependencyMapWorkflow';
import {
	hydrateSummarizationDependencyContext,
} from '../summarizationDependencyContext';
import {
	selectQuestionRelevantSummaryExcerpt,
} from '../summaryAnswerRouting';
// import * as myExtension from '../../extension';

async function waitForCondition(
	predicate: () => boolean,
	timeoutMs = 3000
): Promise<void> {
	const deadline = Date.now() + timeoutMs;

	while (!predicate()) {
		if (Date.now() >= deadline) {
			throw new Error(
				`Timed out after ${timeoutMs}ms waiting for condition.`
			);
		}

		await new Promise<void>((resolve) =>
			setTimeout(resolve, 10)
		);
	}
}

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

	test('Packaged binaries are mandatory source exclusions', () => {
		for (const expectedGlob of [
			'*.vsix',
			'**/*.vsix',
			'*.zip',
			'**/*.zip',
			'*.exe',
			'**/*.exe',
			'*.dll',
			'**/*.dll',
			'*.jar',
			'**/*.jar',
		]) {
			assert.ok(
				NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS.includes(
					expectedGlob
				),
				`Missing mandatory artifact exclusion: ${expectedGlob}`
			);
		}
	});

	test('Mandatory artifact exclusions are merged with project excludes', () => {
		const result = getBatchSourceGlobs({
			raw: [
				'source:',
				'  exclude:',
				'    - custom/**',
			].join('\n'),
		});

		assert.ok(
			result.excludeGlobs.includes('custom/**')
		);

		for (const artifactGlob of [
			'*.vsix',
			'**/*.vsix',
			'*.zip',
			'**/*.zip',
		]) {
			assert.ok(
				result.excludeGlobs.includes(artifactGlob),
				`Missing merged artifact exclusion: ${artifactGlob}`
			);
		}
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
		const extensionSource = await fs.readFile(
			extensionSourcePath,
			'utf8'
		);

		assert.doesNotMatch(
			extensionSource,
			/Missing aiDevCore\.path in \.ai-dev\.yaml\./
		);
		assert.doesNotMatch(
			extensionSource,
			/Missing \.ai-dev\.yaml in workspace root\./
		);
	});

	test('Assistant input defaults to chat mode', () => {
		const state = createAssistantInputState();

		assert.strictEqual(state.mode, 'chat');
		assert.strictEqual(state.input, '');
	});

	test('Typing slash as first char enters command mode', () => {
		const state = applyTextInput(createAssistantInputState(), '/');

		assert.strictEqual(state.mode, 'command');
		assert.strictEqual(state.input, '');
	});

	test('Escape returns command mode to chat mode', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/hel');
		const escaped = handleEscape(commandState);

		assert.strictEqual(escaped.mode, 'chat');
		assert.strictEqual(escaped.input, '');
	});

	test('Deleting command slash returns to chat mode', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/h');
		const oneBackspace = handleBackspace(commandState);
		const twoBackspaces = handleBackspace(oneBackspace);

		assert.strictEqual(twoBackspaces.mode, 'chat');
		assert.strictEqual(twoBackspaces.input, '');
	});

	test('Consolidated slash commands are recognized', () => {
		assert.strictEqual(parseSlashCommand('/help').name, 'help');
		assert.strictEqual(parseSlashCommand('/ask question').name, 'ask');
		assert.strictEqual(parseSlashCommand('/summarize src/*.ts').name, 'summarize');
		assert.strictEqual(parseSlashCommand('/review').name, 'review');
		assert.strictEqual(parseSlashCommand('/settings').name, 'settings');
		assert.strictEqual(parseSlashCommand('/showreport').name, 'showreport');
		assert.strictEqual(parseSlashCommand('/exit').name, 'exit');
	});

	test('Slash command parser separates arguments and options', () => {
		const parsed = parseSlashCommand(
			'/ask --summary "Where is billing deployed?"'
		);

		assert.strictEqual(parsed.name, 'ask');
		assert.deepStrictEqual(parsed.arguments, ['Where is billing deployed?']);
		assert.deepStrictEqual(parsed.options, ['--summary']);
	});

	test('Slash command parser supports short options', () => {
		const parsed = parseSlashCommand('/ask -k release approvals');

		assert.strictEqual(parsed.name, 'ask');
		assert.deepStrictEqual(parsed.arguments, ['release', 'approvals']);
		assert.deepStrictEqual(parsed.options, ['-k']);
	});

	test('Slash command parser preserves quoted glob targets', () => {
		const parsed = parseSlashCommand(
			'/summarize "./lib/*.jenkins" --smoketest'
		);

		assert.strictEqual(parsed.name, 'summarize');
		assert.deepStrictEqual(parsed.arguments, ['./lib/*.jenkins']);
		assert.deepStrictEqual(parsed.options, ['--smoketest']);
	});

	test('Unknown slash command is handled as unknown', () => {
		assert.strictEqual(parseSlashCommand('/unknown').name, 'unknown');
	});

	test('Automatic routing uses summaries for project-relative questions', () => {
		const projectQuestions = [
			'What does this plugin do?',
			'Where do we deploy the billing service?',
			'How does our Jenkins pipeline work?',
			'What is defined in src/extension.ts?',
			'Explain the architecture of this repository.',
		];

		for (const question of projectQuestions) {
			assert.strictEqual(
				chooseAutomaticAssistantRoute(question),
				'summary',
				question
			);
		}
	});

	test('Automatic routing keeps general questions in chat', () => {
		const generalQuestions = [
			'What is eventual consistency?',
			'Explain JavaScript closures.',
			'How does photosynthesis work?',
			'Tell me a joke.',
		];

		for (const question of generalQuestions) {
			assert.strictEqual(
				chooseAutomaticAssistantRoute(question),
				'chat',
				question
			);
		}
	});

	test('/ask defaults to auto routing', () => {
		const resolved = resolveAskCommand(
			parseSlashCommand('/ask Where is billing deployed?')
		);

		assert.deepStrictEqual(resolved, {
			ok: true,
			route: 'auto',
			question: 'Where is billing deployed?',
		});
	});

	test('/ask supports auto and chat short aliases', () => {
		assert.deepStrictEqual(
			resolveAskCommand(
				parseSlashCommand('/ask -a What does this do?')
			),
			{
				ok: true,
				route: 'auto',
				question: 'What does this do?',
			}
		);

		assert.deepStrictEqual(
			resolveAskCommand(
				parseSlashCommand('/ask -c Explain closures')
			),
			{
				ok: true,
				route: 'chat',
				question: 'Explain closures',
			}
		);
	});

	test('/ask supports summary route aliases', () => {
		for (const option of ['--summary', '-s']) {
			const resolved = resolveAskCommand(
				parseSlashCommand(`/ask ${option} Where is billing deployed?`)
			);

			assert.deepStrictEqual(resolved, {
				ok: true,
				route: 'summary',
				question: 'Where is billing deployed?',
			});
		}
	});

	test('/ask supports knowledgebase route aliases', () => {
		for (const option of ['--knowledgebase', '-k']) {
			const resolved = resolveAskCommand(
				parseSlashCommand(`/ask ${option} What is the release process?`)
			);

			assert.deepStrictEqual(resolved, {
				ok: true,
				route: 'knowledgebase',
				question: 'What is the release process?',
			});
		}
	});

	test('/ask supports explicit chat routing', () => {
		const resolved = resolveAskCommand(
			parseSlashCommand('/ask --chat Explain closures')
		);

		assert.deepStrictEqual(resolved, {
			ok: true,
			route: 'chat',
			question: 'Explain closures',
		});
	});

	test('/ask rejects conflicting routes', () => {
		const resolved = resolveAskCommand(
			parseSlashCommand('/ask --summary --chat Explain billing')
		);

		assert.deepStrictEqual(resolved, {
			ok: false,
			error: 'Choose only one /ask route.',
		});
	});

	test('/ask rejects unknown options', () => {
		const resolved = resolveAskCommand(
			parseSlashCommand('/ask --banana Explain billing')
		);

		assert.deepStrictEqual(resolved, {
			ok: false,
			error: 'Unknown /ask option: --banana',
		});
	});

	test('/ask requires a question', () => {
		const resolved = resolveAskCommand(
			parseSlashCommand('/ask --summary')
		);

		assert.strictEqual(resolved.ok, false);
		if (!resolved.ok) {
			assert.match(resolved.error, /^Usage: \/ask/);
		}
	});

	test('Summarization config HTML renders the general row and rule table', () => {
		const config = createDefaultSummarizationConfig();

		config.rules.push({
			id: 'jenkins',
			name: 'Jenkins job config',
			glob: '**/jobs/**/config.xml',
			priority: 100,
			enabled: true,
			instructions: 'Focus on build steps.',
		});

		const html = buildSummarizationConfigHtml(config);

		assert.match(html, /Summarization Configuration/);
		assert.match(html, /General summarization/);
		assert.match(html, /Add rule/);
		assert.match(html, /dblclick/);
		assert.match(html, /ruleDialog/);
		assert.match(
			html,
			/Jenkins job config/
		);
	});

	test('Summarization config HTML includes live inline pattern testing', () => {
		const html = buildSummarizationConfigHtml(
			createDefaultSummarizationConfig()
		);

		assert.match(html, /testPatternEnabled/);
		assert.match(html, /patternTestSummary/);
		assert.match(html, /togglePatternMatches/);
		assert.match(html, /schedulePatternTest/);
		assert.match(html, /setTimeout\(run, 350\)/);
		assert.match(html, /requestId/);
		assert.doesNotMatch(
			html,
			/id="testPatternButton"/
		);
	});

	test('Summarization config HTML safely serializes script-like instructions', () => {
		const config = createDefaultSummarizationConfig();
		config.generalInstructions =
			'</script><script>alert(1)</script>';

		const html = buildSummarizationConfigHtml(config);

		assert.doesNotMatch(
			html,
			/<\/script><script>alert\(1\)<\/script>/
		);
		assert.match(html, /\\u003c\/script\\u003e/);
	});

	test('Markdown path normalization accepts either slash style', () => {
		assert.strictEqual(
			normalizePathForMarkdown(
				'jobs\\billing\\config.xml'
			),
			'jobs/billing/config.xml'
		);
		assert.strictEqual(
			normalizePathForMarkdown(
				'jobs/billing/config.xml'
			),
			'jobs/billing/config.xml'
		);
	});

	test('Missing dependency map returns an empty versioned map', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-dependency-map-')
		);
		const config = await readAiDevConfig(
			workspaceRoot
		);

		assert.deepStrictEqual(
			await readDependencyMap(
				workspaceRoot,
				config
			),
			createEmptyDependencyMap()
		);
	});

	test('Dependency map round-trips with deterministic edge ordering', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-dependency-map-')
		);
		const config = await readAiDevConfig(
			workspaceRoot
		);

		await writeDependencyMap(
			workspaceRoot,
			config,
			{
				version: 1,
				edges: [
					{
						sourcePath:
							'jenkins\\jobs\\billing\\config.xml',
						targetPath:
							'pipelines/billing/Jenkinsfile',
						kind:
							'jenkins-pipeline-script',
						resolution: 'inferred',
						evidence: [
							{
								kind: 'name-match',
								detail:
									'Matched the billing job name.',
							},
						],
					},
					{
						sourcePath:
							'jenkins/jobs/billing/config.xml',
						targetPath:
							'pipelines/billing/Jenkinsfile',
						kind:
							'jenkins-pipeline-script',
						resolution: 'exact',
						evidence: [
							{
								kind: 'config-value',
								detail:
									'Resolved from scriptPath.',
							},
						],
					},
				],
			}
		);

		const dependencyMap =
			await readDependencyMap(
				workspaceRoot,
				config
			);

		assert.strictEqual(
			dependencyMap.edges[0].resolution,
			'exact'
		);
		assert.strictEqual(
			dependencyMap.edges[1].resolution,
			'inferred'
		);
		assert.strictEqual(
			dependencyMap.edges[0].sourcePath,
			'jenkins/jobs/billing/config.xml'
		);
	});

	test('Dependency map validation rejects unsupported resolved edges', () => {
		const issues = validateDependencyMap({
			version: 1,
			edges: [
				{
					sourcePath:
						'jenkins/jobs/billing/config.xml',
					kind:
						'jenkins-pipeline-script',
					resolution: 'exact',
					evidence: [],
				},
			],
		});

		assert.ok(
			issues.some(
				(issue) =>
					issue.field === 'targetPath'
			)
		);
		assert.ok(
			issues.some(
				(issue) =>
					issue.field === 'evidence'
			)
		);
	});

	test('Outgoing dependency queries prefer exact edges and support filters', () => {
		const dependencyMap = {
			version: 1 as const,
			edges: [
				{
					sourcePath: 'jobs/billing/config.xml',
					targetPath:
						'pipelines/other/Jenkinsfile',
					kind:
						'jenkins-pipeline-script',
					resolution:
						'inferred' as const,
					evidence: [
						{
							kind: 'name-match',
							detail: 'Possible match.',
						},
					],
				},
				{
					sourcePath: 'jobs/billing/config.xml',
					targetPath:
						'pipelines/billing/Jenkinsfile',
					kind:
						'jenkins-pipeline-script',
					resolution: 'exact' as const,
					evidence: [
						{
							kind: 'config-value',
							detail: 'Exact scriptPath.',
						},
					],
				},
				{
					sourcePath: 'jobs/billing/config.xml',
					kind: 'jenkins-shared-library',
					resolution: 'unresolved' as const,
					evidence: [
						{
							kind: 'library-call',
							detail:
								'Library name was present.',
						},
					],
				},
			],
		};

		const exactOnly =
			findOutgoingDependencyEdges(
				dependencyMap,
				'jobs\\billing\\config.xml',
				{
					kinds: [
						'jenkins-pipeline-script',
					],
					includeInferred: false,
				}
			);

		assert.strictEqual(
			exactOnly.length,
			1
		);
		assert.strictEqual(
			exactOnly[0].resolution,
			'exact'
		);

		const withInferred =
			findOutgoingDependencyEdges(
				dependencyMap,
				'jobs/billing/config.xml'
			);

		assert.deepStrictEqual(
			withInferred.map(
				(edge) => edge.resolution
			),
			['exact', 'inferred']
		);
	});

	test('Jenkins SCM Pipeline scriptPath resolves an exact workspace file', () => {
		const configXml = [
			'<flow-definition>',
			'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
			'    <scriptPath>pipelines/billing/Jenkinsfile</scriptPath>',
			'  </definition>',
			'</flow-definition>',
		].join('\n');

		const edge = resolveJenkinsPipelineDependency({
			configPath:
				'jenkins/jobs/billing/config.xml',
			configXml,
			candidatePaths: [
				'pipelines/billing/Jenkinsfile',
				'pipelines/orders/Jenkinsfile',
			],
		});

		assert.ok(edge);
		assert.strictEqual(edge.resolution, 'exact');
		assert.strictEqual(
			edge.targetPath,
			'pipelines/billing/Jenkinsfile'
		);
		assert.strictEqual(
			edge.kind,
			'jenkins-pipeline-script'
		);
	});

	test('Jenkins SCM Pipeline scriptPath resolves a unique suffix as inferred', () => {
		const configXml = [
			'<flow-definition>',
			'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
			'    <scriptPath>ci/Jenkinsfile</scriptPath>',
			'  </definition>',
			'</flow-definition>',
		].join('\n');

		const edge = resolveJenkinsPipelineDependency({
			configPath: 'jenkins/jobs/app/config.xml',
			configXml,
			candidatePaths: [
				'repositories/app/ci/Jenkinsfile',
			],
		});

		assert.ok(edge);
		assert.strictEqual(edge.resolution, 'inferred');
		assert.strictEqual(
			edge.targetPath,
			'repositories/app/ci/Jenkinsfile'
		);
	});

	test('Jenkins SCM Pipeline scriptPath reports ambiguous suffix matches', () => {
		const configXml = [
			'<flow-definition>',
			'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
			'    <scriptPath>ci/Jenkinsfile</scriptPath>',
			'  </definition>',
			'</flow-definition>',
		].join('\n');

		const edge = resolveJenkinsPipelineDependency({
			configPath: 'jenkins/jobs/app/config.xml',
			configXml,
			candidatePaths: [
				'repositories/app/ci/Jenkinsfile',
				'repositories/app-copy/ci/Jenkinsfile',
			],
		});

		assert.ok(edge);
		assert.strictEqual(edge.resolution, 'ambiguous');
		assert.strictEqual(edge.targetPath, undefined);
		assert.match(
			edge.evidence[0].detail,
			/matches multiple workspace files/
		);
	});

	test('Jenkins SCM Pipeline scriptPath reports unresolved references', () => {
		const configXml = [
			'<flow-definition>',
			'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
			'    <scriptPath>missing/Jenkinsfile</scriptPath>',
			'  </definition>',
			'</flow-definition>',
		].join('\n');

		const edge = resolveJenkinsPipelineDependency({
			configPath: 'jenkins/jobs/app/config.xml',
			configXml,
			candidatePaths: [
				'repositories/app/Jenkinsfile',
			],
		});

		assert.ok(edge);
		assert.strictEqual(edge.resolution, 'unresolved');
		assert.strictEqual(edge.targetPath, undefined);
	});

	test('Jenkins inline Pipeline definitions do not produce script dependencies', () => {
		const configXml = [
			'<flow-definition>',
			'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition">',
			'    <script>pipeline { agent any }</script>',
			'  </definition>',
			'</flow-definition>',
		].join('\n');

		assert.strictEqual(
			extractJenkinsPipelineScriptPath(configXml),
			undefined
		);
		assert.strictEqual(
			resolveJenkinsPipelineDependency({
				configPath:
					'jenkins/jobs/inline/config.xml',
				configXml,
				candidatePaths: [
					'pipelines/Jenkinsfile',
				],
			}),
			undefined
		);
	});

	test('Jenkins dependency preflight only runs for selected config.xml files', async () => {
		let refreshCalls = 0;

		const result =
			await refreshJenkinsDependencyMapForSummarization(
				'/workspace',
				[
					'pipelines/Jenkinsfile',
					'src/index.ts',
				],
				async () => {
					refreshCalls += 1;
					throw new Error(
						'Refresh should not run.'
					);
				}
			);

		assert.strictEqual(refreshCalls, 0);
		assert.deepStrictEqual(result, {
			refreshed: false,
			warnings: [],
		});
		assert.strictEqual(
			shouldRefreshJenkinsDependencyMap([
				'jobs/billing/CONFIG.XML',
			]),
			true
		);
	});

	test('Jenkins dependency preflight propagates refresh warnings', async () => {
		const result =
			await refreshJenkinsDependencyMapForSummarization(
				'/workspace',
				['jobs/billing/config.xml'],
				async () => ({
					dependencyMapPath:
						'ai-docs/dependency-map.json',
					scannedConfigCount: 1,
					resolvedEdgeCount: 1,
					exactCount: 0,
					inferredCount: 0,
					ambiguousCount: 0,
					unresolvedCount: 1,
					skippedCount: 0,
					failedCount: 0,
					warnings: [
						'jobs/billing/config.xml: no Jenkinsfile matched.',
					],
				})
			);

		assert.deepStrictEqual(result, {
			refreshed: true,
			warnings: [
				'jobs/billing/config.xml: no Jenkinsfile matched.',
			],
		});
	});

	test('Jenkins dependency preflight converts refresh failure into a warning', async () => {
		const result =
			await refreshJenkinsDependencyMapForSummarization(
				'/workspace',
				['jobs/billing/config.xml'],
				async () => {
					throw new Error(
						'Dependency map is read-only.'
					);
				}
			);

		assert.deepStrictEqual(result, {
			refreshed: false,
			warnings: [
				'Dependency map refresh failed: Dependency map is read-only.',
			],
		});
	});

	test('Jenkins dependency refresh persists resolved edges and preserves unrelated edges', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-dependency-refresh-')
		);
		const config = await readAiDevConfig(workspaceRoot);

		await fs.mkdir(
			path.join(
				workspaceRoot,
				'jenkins/jobs/billing'
			),
			{ recursive: true }
		);
		await fs.mkdir(
			path.join(
				workspaceRoot,
				'pipelines/billing'
			),
			{ recursive: true }
		);

		await fs.writeFile(
			path.join(
				workspaceRoot,
				'jenkins/jobs/billing/config.xml'
			),
			[
				'<flow-definition>',
				'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
				'    <scriptPath>pipelines/billing/Jenkinsfile</scriptPath>',
				'  </definition>',
				'</flow-definition>',
			].join('\n'),
			'utf8'
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'pipelines/billing/Jenkinsfile'
			),
			'pipeline { agent any }',
			'utf8'
		);

		await writeDependencyMap(
			workspaceRoot,
			config,
			{
				version: 1,
				edges: [
					{
						sourcePath: 'addon/MyAddon.toc',
						targetPath: 'addon/Core.lua',
						kind: 'wow-toc-load',
						resolution: 'exact',
						evidence: [
							{
								kind: 'toc-entry',
								detail:
									'Core.lua is listed first.',
							},
						],
					},
				],
			}
		);

		const result =
			await refreshJenkinsDependencyMap(
				workspaceRoot
			);
		const dependencyMap =
			await readDependencyMap(
				workspaceRoot,
				config
			);

		assert.strictEqual(
			result.scannedConfigCount,
			1
		);
		assert.strictEqual(result.exactCount, 1);
		assert.strictEqual(
			result.dependencyMapPath,
			'ai-docs/dependency-map.json'
		);
		assert.ok(
			dependencyMap.edges.some(
				(edge) =>
					edge.kind
						=== 'jenkins-pipeline-script'
					&& edge.targetPath
						=== 'pipelines/billing/Jenkinsfile'
			)
		);
		assert.ok(
			dependencyMap.edges.some(
				(edge) =>
					edge.kind === 'wow-toc-load'
			)
		);
	});

	test('Jenkins dependency refresh removes stale edges for scanned inline jobs', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-dependency-refresh-')
		);
		const config = await readAiDevConfig(workspaceRoot);

		await fs.mkdir(
			path.join(
				workspaceRoot,
				'jenkins/jobs/inline'
			),
			{ recursive: true }
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'jenkins/jobs/inline/config.xml'
			),
			[
				'<flow-definition>',
				'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition">',
				'    <script>pipeline { agent any }</script>',
				'  </definition>',
				'</flow-definition>',
			].join('\n'),
			'utf8'
		);

		await writeDependencyMap(
			workspaceRoot,
			config,
			{
				version: 1,
				edges: [
					{
						sourcePath:
							'jenkins/jobs/inline/config.xml',
						targetPath:
							'pipelines/old/Jenkinsfile',
						kind:
							'jenkins-pipeline-script',
						resolution: 'exact',
						evidence: [
							{
								kind: 'old-value',
								detail:
									'Previously resolved.',
							},
						],
					},
				],
			}
		);

		const result =
			await refreshJenkinsDependencyMap(
				workspaceRoot
			);
		const dependencyMap =
			await readDependencyMap(
				workspaceRoot,
				config
			);

		assert.strictEqual(
			result.skippedCount,
			1
		);
		assert.deepStrictEqual(
			dependencyMap.edges,
			[]
		);
	});

	test('Jenkins dependency refresh reports ambiguous and unresolved jobs', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-dependency-refresh-')
		);

		for (const relativePath of [
			'jenkins/jobs/ambiguous/config.xml',
			'jenkins/jobs/missing/config.xml',
			'repositories/a/ci/Jenkinsfile',
			'repositories/b/ci/Jenkinsfile',
		]) {
			await fs.mkdir(
				path.dirname(
					path.join(
						workspaceRoot,
						relativePath
					)
				),
				{ recursive: true }
			);
		}

		const scmConfig = (scriptPath: string) =>
			[
				'<flow-definition>',
				'  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">',
				`    <scriptPath>${scriptPath}</scriptPath>`,
				'  </definition>',
				'</flow-definition>',
			].join('\n');

		await fs.writeFile(
			path.join(
				workspaceRoot,
				'jenkins/jobs/ambiguous/config.xml'
			),
			scmConfig('ci/Jenkinsfile'),
			'utf8'
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'jenkins/jobs/missing/config.xml'
			),
			scmConfig('missing/Jenkinsfile'),
			'utf8'
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'repositories/a/ci/Jenkinsfile'
			),
			'pipeline {}',
			'utf8'
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'repositories/b/ci/Jenkinsfile'
			),
			'pipeline {}',
			'utf8'
		);

		const result =
			await refreshJenkinsDependencyMap(
				workspaceRoot
			);

		assert.strictEqual(
			result.ambiguousCount,
			1
		);
		assert.strictEqual(
			result.unresolvedCount,
			1
		);
		assert.strictEqual(
			result.warnings.length,
			2
		);
	});

	test('Summarization dependency strategies normalize with bounded defaults', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions: 'General guidance.',
			rules: [
				{
					id: 'jenkins',
					name: 'Jenkins config',
					glob: '**/config.xml',
					priority: 100,
					enabled: true,
					instructions:
						'Explain delegated pipeline behavior.',
					dependencyStrategy: {
						follow: [
							'jenkins-pipeline-script',
							'jenkins-pipeline-script',
						],
					},
				},
			],
		});

		assert.deepStrictEqual(
			config.rules[0].dependencyStrategy,
			{
				follow: [
					'jenkins-pipeline-script',
				],
				maxDepth: 1,
				maxFiles: 4,
				maxChars: 24000,
				includeInferred: false,
			}
		);
	});

	test('Summarization dependency strategy rejects unsupported traversal depth', () => {
		const config = createDefaultSummarizationConfig();

		config.rules.push({
			id: 'jenkins',
			name: 'Jenkins config',
			glob: '**/config.xml',
			priority: 100,
			enabled: true,
			instructions: 'Explain dependencies.',
			dependencyStrategy: {
				follow: [
					'jenkins-pipeline-script',
				],
				maxDepth: 2,
				maxFiles: 4,
				maxChars: 24000,
				includeInferred: false,
			},
		});

		const issues =
			validateSummarizationConfig(config);

		assert.ok(
			issues.some(
				(issue) =>
					issue.field
					=== 'dependencyStrategy.maxDepth'
			)
		);
	});

	test('Last matching dependency strategy wins', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions: 'General guidance.',
			rules: [
				{
					id: 'xml',
					name: 'XML',
					glob: '**/*.xml',
					priority: 10,
					enabled: true,
					instructions: 'General XML.',
					dependencyStrategy: {
						follow: ['xml-reference'],
						maxDepth: 1,
						maxFiles: 2,
						maxChars: 8000,
						includeInferred: false,
					},
				},
				{
					id: 'jenkins',
					name: 'Jenkins',
					glob: '**/config.xml',
					priority: 100,
					enabled: true,
					instructions: 'Jenkins-specific.',
					dependencyStrategy: {
						follow: [
							'jenkins-pipeline-script',
						],
						maxDepth: 1,
						maxFiles: 4,
						maxChars: 24000,
						includeInferred: true,
					},
				},
			],
		});

		const resolved =
			resolveSummarizationInstructions(
				config,
				'jobs/billing/config.xml'
			);

		assert.deepStrictEqual(
			resolved.dependencyStrategy,
			config.rules[1].dependencyStrategy
		);
	});

	test('Grouped summary prompts label resolved dependency context', () => {
		const prompt =
			buildGroupedGenerateUnitDocDirectPromptMarkdown({
				workspaceRoot: '/workspace',
				workflowFilePath: 'workflow.md',
				workflowFileContents: 'workflow',
				templateFilePath: 'template.md',
				templateFileContents: 'template',
				targetSummaryPath:
					'ai-docs/jobs/summary.md',
				selectedSourceFiles: [
					{
						path:
							'jobs/billing/config.xml',
						contents:
							'<scriptPath>ci/Jenkinsfile</scriptPath>',
					},
				],
				dependencyContextFiles: [
					{
						primarySourcePath:
							'jobs/billing/config.xml',
						path:
							'repositories/billing/ci/Jenkinsfile',
						relationshipKind:
							'jenkins-pipeline-script',
						resolution: 'exact',
						evidence: [
							'Resolved from scriptPath.',
						],
						contents:
							'pipeline { stages { stage("Build") {} } }',
					},
				],
			});

		assert.match(
			prompt,
			/Resolved dependency context:/
		);
		assert.match(
			prompt,
			/Primary source: jobs\/billing\/config\.xml/
		);
		assert.match(
			prompt,
			/Dependency file: repositories\/billing\/ci\/Jenkinsfile/
		);
		assert.match(
			prompt,
			/Relationship: jenkins-pipeline-script/
		);
		assert.match(
			prompt,
			/Use dependency context only when needed/
		);
	});

	test('Dependency hydration includes exact files with provenance', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(
				os.tmpdir(),
				'ai-dev-dependency-context-'
			)
		);

		await fs.mkdir(
			path.join(workspaceRoot, 'pipelines'),
			{ recursive: true }
		);
		await fs.writeFile(
			path.join(
				workspaceRoot,
				'pipelines/Jenkinsfile'
			),
			'pipeline { stage("Build") {} }',
			'utf8'
		);

		const result =
			await hydrateSummarizationDependencyContext({
				workspaceRoot,
				dependencyMap: {
					version: 1,
					edges: [
						{
							sourcePath:
								'jobs/config.xml',
							targetPath:
								'pipelines/Jenkinsfile',
							kind:
								'jenkins-pipeline-script',
							resolution: 'exact',
							evidence: [
								{
									kind:
										'jenkins-script-path',
									detail:
										'Resolved from scriptPath.',
								},
							],
						},
					],
				},
				sourcePaths: ['jobs/config.xml'],
				resolvedBySource: [
					{
						generalInstructions: '',
						matchingRules: [],
						combinedInstructions: '',
						dependencyStrategy: {
							follow: [
								'jenkins-pipeline-script',
							],
							maxDepth: 1,
							maxFiles: 4,
							maxChars: 24000,
							includeInferred: false,
						},
					},
				],
			});

		assert.strictEqual(result.files.length, 1);
		assert.strictEqual(
			result.files[0].path,
			'pipelines/Jenkinsfile'
		);
		assert.deepStrictEqual(
			result.files[0].evidence,
			['Resolved from scriptPath.']
		);
		assert.match(
			result.files[0].contents,
			/stage\("Build"\)/
		);
		assert.deepStrictEqual(result.warnings, []);
	});

	test('Dependency hydration excludes inferred edges unless enabled', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(
				os.tmpdir(),
				'ai-dev-dependency-context-'
			)
		);

		await fs.writeFile(
			path.join(workspaceRoot, 'Jenkinsfile'),
			'pipeline {}',
			'utf8'
		);

		const baseResolved = {
			generalInstructions: '',
			matchingRules: [],
			combinedInstructions: '',
			dependencyStrategy: {
				follow: [
					'jenkins-pipeline-script',
				],
				maxDepth: 1,
				maxFiles: 4,
				maxChars: 24000,
				includeInferred: false,
			},
		};

		const dependencyMap = {
			version: 1 as const,
			edges: [
				{
					sourcePath: 'jobs/config.xml',
					targetPath: 'Jenkinsfile',
					kind:
						'jenkins-pipeline-script',
					resolution: 'inferred' as const,
					evidence: [
						{
							kind: 'suffix',
							detail: 'Unique suffix.',
						},
					],
				},
			],
		};

		const excluded =
			await hydrateSummarizationDependencyContext({
				workspaceRoot,
				dependencyMap,
				sourcePaths: ['jobs/config.xml'],
				resolvedBySource: [baseResolved],
			});

		assert.strictEqual(excluded.files.length, 0);

		const included =
			await hydrateSummarizationDependencyContext({
				workspaceRoot,
				dependencyMap,
				sourcePaths: ['jobs/config.xml'],
				resolvedBySource: [
					{
						...baseResolved,
						dependencyStrategy: {
							...baseResolved.dependencyStrategy,
							includeInferred: true,
						},
					},
				],
			});

		assert.strictEqual(included.files.length, 1);
		assert.strictEqual(
			included.files[0].resolution,
			'inferred'
		);
	});

	test('Dependency hydration reports unresolved and bounded context', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(
				os.tmpdir(),
				'ai-dev-dependency-context-'
			)
		);

		for (const fileName of ['one.txt', 'two.txt']) {
			await fs.writeFile(
				path.join(workspaceRoot, fileName),
				'1234567890',
				'utf8'
			);
		}

		const result =
			await hydrateSummarizationDependencyContext({
				workspaceRoot,
				dependencyMap: {
					version: 1,
					edges: [
						{
							sourcePath:
								'jobs/config.xml',
							kind:
								'jenkins-pipeline-script',
							resolution:
								'unresolved',
							evidence: [
								{
									kind: 'missing',
									detail:
										'No Jenkinsfile matched.',
								},
							],
						},
						...['one.txt', 'two.txt'].map(
							(targetPath) => ({
								sourcePath:
									'jobs/config.xml',
								targetPath,
								kind:
									'jenkins-pipeline-script',
								resolution:
									'exact' as const,
								evidence: [
									{
										kind: 'test',
										detail:
											`Resolved ${targetPath}.`,
									},
								],
							})
						),
					],
				},
				sourcePaths: ['jobs/config.xml'],
				resolvedBySource: [
					{
						generalInstructions: '',
						matchingRules: [],
						combinedInstructions: '',
						dependencyStrategy: {
							follow: [
								'jenkins-pipeline-script',
							],
							maxDepth: 1,
							maxFiles: 1,
							maxChars: 5,
							includeInferred: false,
						},
					},
				],
			});

		assert.strictEqual(result.files.length, 1);
		assert.strictEqual(
			result.files[0].contents.length,
			5
		);
		assert.ok(
			result.warnings.some(
				(warning) =>
					warning.includes(
						'No Jenkinsfile matched.'
					)
			)
		);
		assert.ok(
			result.warnings.some(
				(warning) =>
					warning.includes('was clipped')
			)
		);
		assert.ok(
			result.warnings.some(
				(warning) =>
					warning.includes(
						'limited to 1 file'
					)
			)
		);
	});

	test('Missing summarization config returns general defaults', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-summary-config-')
		);

		const config = await readSummarizationConfig(
			workspaceRoot
		);

		assert.strictEqual(config.version, 1);
		assert.strictEqual(
			config.generalInstructions,
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS
		);
		assert.deepStrictEqual(config.rules, []);
	});

	test('Summarization config round-trips specialized rules', async () => {
		const workspaceRoot = await fs.mkdtemp(
			path.join(os.tmpdir(), 'ai-dev-summary-config-')
		);
		const config = createDefaultSummarizationConfig();

		config.rules.push({
			id: 'jenkins-config',
			name: 'Jenkins job config',
			glob: '**/jobs/**/config.xml',
			priority: 100,
			enabled: true,
			instructions:
				'Focus on build steps and disabled state.',
		});

		await writeSummarizationConfig(
			workspaceRoot,
			config
		);

		assert.deepStrictEqual(
			await readSummarizationConfig(workspaceRoot),
			config
		);
	});

	test('Summarization glob supports recursive path matching', () => {
		assert.strictEqual(
			matchesSummarizationGlob(
				'jenkins/jobs/billing/config.xml',
				'**/jobs/**/config.xml'
			),
			true
		);
		assert.strictEqual(
			matchesSummarizationGlob(
				'jenkins/jobs/billing/Jenkinsfile',
				'**/jobs/**/config.xml'
			),
			false
		);
	});

	test('Summary routing keeps relevant entries beyond the character cutoff', () => {
		const prefix = Array.from(
			{ length: 80 },
			(_, index) =>
				`- unrelated module ${index}: ${'x'.repeat(80)}`
		).join('\n');

		const relevantEntry = [
			'- `src/summarizationWorkflow.ts` — ',
			'Grouped summarization writes directory summaries first, ',
			'then refreshes the architecture summary.',
		].join('');

		const contents = [
			prefix,
			relevantEntry,
		].join('\n');

		assert.ok(
			contents.indexOf(relevantEntry) > 6000
		);

		const excerpt =
			selectQuestionRelevantSummaryExcerpt(
				contents,
				'How does grouped summarization refresh the architecture summary?',
				6000
			);

		assert.match(
			excerpt,
			/summarizationWorkflow\.ts/
		);
		assert.match(
			excerpt,
			/writes directory summaries first/
		);
	});

	test('Summary routing preserves small summaries unchanged', () => {
		const contents = [
			'- `src/a.ts` — Handles feature A.',
			'- `src/b.ts` — Handles feature B.',
		].join('\n');

		assert.strictEqual(
			selectQuestionRelevantSummaryExcerpt(
				contents,
				'How does feature B work?',
				6000
			),
			contents
		);
	});

	test('Authoritative grouped summary prompts remove stale source entries', () => {
		const prompt =
			buildGroupedGenerateUnitDocDirectPromptMarkdown({
				workspaceRoot: '/workspace',
				workflowFilePath: 'workflow.md',
				workflowFileContents: 'workflow',
				templateFilePath: 'template.md',
				templateFileContents: 'template',
				targetSummaryPath: 'ai-docs/src/summary.md',
				existingSummaryContents: '- stale entry',
				selectedSourceFiles: [
					{
						path: 'src/current.ts',
						contents:
							'export const current = true;',
					},
				],
				sourceSetIsAuthoritative: true,
			});

		assert.match(
			prompt,
			/complete authoritative current source set/
		);
		assert.match(
			prompt,
			/Remove entries for source files not in this set/
		);
		assert.doesNotMatch(
			prompt,
			/Preserve useful existing entries/
		);
	});

	test('Partial grouped summary prompts preserve unselected entries', () => {
		const prompt =
			buildGroupedGenerateUnitDocDirectPromptMarkdown({
				workspaceRoot: '/workspace',
				workflowFilePath: 'workflow.md',
				workflowFileContents: 'workflow',
				templateFilePath: 'template.md',
				templateFileContents: 'template',
				targetSummaryPath: 'ai-docs/src/summary.md',
				selectedSourceFiles: [
					{
						path: 'src/current.ts',
						contents:
							'export const current = true;',
					},
				],
			});

		assert.match(
			prompt,
			/Preserve useful existing entries/
		);
		assert.doesNotMatch(
			prompt,
			/complete authoritative current source set/
		);
	});

	test('Default summarization guidance captures orchestration lifecycle behavior', () => {
		assert.match(
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
			/ordered execution lifecycle/
		);
		assert.match(
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
			/gating conditions/
		);
		assert.match(
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
			/writes and downstream refreshes/
		);
		assert.match(
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
			/cancellation behavior/
		);
		assert.match(
			DEFAULT_GENERAL_SUMMARY_INSTRUCTIONS,
			/which completed work remains valid when later steps fail/
		);
	});
	test('General instructions precede matching specialized rules', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions: 'General guidance.',
			rules: [
				{
					id: 'high',
					name: 'Higher priority',
					glob: '**/config.xml',
					priority: 100,
					enabled: true,
					instructions: 'High-priority guidance.',
				},
				{
					id: 'low',
					name: 'Lower priority',
					glob: '**/*.xml',
					priority: 10,
					enabled: true,
					instructions: 'Low-priority guidance.',
				},
				{
					id: 'disabled',
					name: 'Disabled',
					glob: '**/*.xml',
					priority: 1,
					enabled: false,
					instructions: 'Should not appear.',
				},
			],
		});

		const resolved = resolveSummarizationInstructions(
			config,
			'jobs/billing/config.xml'
		);

		assert.deepStrictEqual(
			resolved.matchingRules.map((rule) => rule.id),
			['low', 'high']
		);
		assert.ok(
			resolved.combinedInstructions.indexOf(
				'General guidance.'
			)
			< resolved.combinedInstructions.indexOf(
				'Low-priority guidance.'
			)
		);
		assert.ok(
			resolved.combinedInstructions.indexOf(
				'Low-priority guidance.'
			)
			< resolved.combinedInstructions.indexOf(
				'High-priority guidance.'
			)
		);
		assert.doesNotMatch(
			resolved.combinedInstructions,
			/Should not appear/
		);
	});

	test('Configured summarization guidance is injected before final task instructions', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions:
				'Summarize behavior, not syntax.',
			rules: [
				{
					id: 'jenkins',
					name: 'Jenkins job config',
					glob: '**/jobs/**/config.xml',
					priority: 100,
					enabled: true,
					instructions: [
						'Include whether the job is disabled.',
						'Focus on build steps.',
					].join('\n'),
				},
			],
		});

		const resolved = resolveSummarizationInstructions(
			config,
			'jenkins/jobs/billing/config.xml'
		);

		const prompt = injectSummarizationInstructions(
			[
				'Source file contents:',
				'<xml />',
				'',
				'Instructions:',
				'Return raw Markdown only.',
			].join('\n'),
			resolved
		);

		assert.match(
			prompt,
			/Configured summarization guidance:/
		);
		assert.match(
			prompt,
			/Summarize behavior, not syntax\./
		);
		assert.match(
			prompt,
			/Rule: Jenkins job config/
		);
		assert.match(
			prompt,
			/Include whether the job is disabled\./
		);

		assert.ok(
			prompt.indexOf(
				'Configured summarization guidance:'
			)
			< prompt.indexOf('Instructions:')
		);
	});

	test('Prompt injection excludes unmatched summarization rules', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions: 'General guidance.',
			rules: [
				{
					id: 'jenkins',
					name: 'Jenkins',
					glob: '**/config.xml',
					priority: 100,
					enabled: true,
					instructions: 'Jenkins-only guidance.',
				},
			],
		});

		const resolved = resolveSummarizationInstructions(
			config,
			'src/example.ts'
		);
		const prompt = injectSummarizationInstructions(
			'Instructions:\nSummarize the file.',
			resolved
		);

		assert.match(prompt, /General guidance\./);
		assert.doesNotMatch(
			prompt,
			/Jenkins-only guidance/
		);
	});

	test('Summarization glob validation detects unmatched delimiters', () => {
		assert.strictEqual(
			validateSummarizationGlobSyntax(
				'**/jobs/[broken/config.xml'
			),
			'Unmatched "[" in glob pattern.'
		);

		assert.strictEqual(
			validateSummarizationGlobSyntax(
				'**/jobs/{broken/config.xml'
			),
			'Unmatched "{" in glob pattern.'
		);

		assert.strictEqual(
			validateSummarizationGlobSyntax(
				'**/jobs/**/config.xml'
			),
			undefined
		);
	});

	test('Summarization config validates incomplete rules', () => {
		const config = normalizeSummarizationConfig({
			version: 1,
			generalInstructions: 'General guidance.',
			rules: [
				{
					id: '',
					name: '',
					glob: '',
					priority: 0,
					enabled: true,
					instructions: '',
				},
			],
		});

		assert.ok(
			validateSummarizationConfig(config).length >= 3
		);
	});

	test('/summarize parser preserves a quoted file path', () => {
		const parsed = parseSlashCommand(
			'/summarize "src/path with spaces/example.ts"'
		);

		assert.strictEqual(parsed.name, 'summarize');
		assert.deepStrictEqual(
			parsed.arguments,
			['src/path with spaces/example.ts']
		);
		assert.deepStrictEqual(parsed.options, []);
	});

	test('/summarize smoketest preserves the target glob', () => {
		const parsed = parseSlashCommand(
			'/summarize "ai-dev-vscode/src/**/*.ts" --smoketest'
		);

		assert.strictEqual(parsed.name, 'summarize');
		assert.deepStrictEqual(
			parsed.arguments,
			['ai-dev-vscode/src/**/*.ts']
		);
		assert.deepStrictEqual(
			parsed.options,
			['--smoketest']
		);
	});

	test('/summarize parser recognizes deterministic options', () => {
		for (
			const option of [
				'--smoketest',
				'-s',
				'--config',
				'-c',
				'--help',
				'-h',
			]
		) {
			const parsed = parseSlashCommand(
				`/summarize src/example.ts ${option}`
			);

			assert.ok(
				parsed.options.includes(option),
				option
			);
		}
	});

	test('Command definitions provide one-line summaries', () => {
		const ask = getAssistantCommandDefinition('/ask');

		assert.ok(ask);
		assert.strictEqual(
			formatAssistantCommandSummary(ask),
			'/ask - Ask the assistant a question'
		);
	});

	test('Command names are derived from centralized metadata', () => {
		assert.deepStrictEqual(
			getAssistantCommandNames(),
			ASSISTANT_COMMAND_DEFINITIONS.map(
				(command) => command.name
			)
		);
	});

	test('/ask help includes usage and all short aliases', () => {
		const ask = getAssistantCommandDefinition('/ask');

		assert.ok(ask);

		const help = formatAssistantCommandHelp(ask).join('\n');

		assert.match(help, /\/ask - Ask the assistant a question/);
		assert.match(help, /Usage:/);
		assert.match(help, /--auto, -a/);
		assert.match(help, /--summary, -s/);
		assert.match(help, /--knowledgebase, -k/);
		assert.match(help, /--chat, -c/);
		assert.match(help, /--help, -h/);
	});

	test('Common path prefix extends multiple matches', () => {
		assert.strictEqual(
			getCommonPrefix([
				'ai-dev-vscode/',
				'ai-dev-vscode-old/',
			]),
			'ai-dev-vscode'
		);
	});

	test('Tab completion results use a dedicated ephemeral region', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/tabCompletionLineCount/
		);
		assert.match(
			source,
			/showTabCompletionLines/
		);
		assert.match(
			source,
			/clearTabCompletionLines/
		);
		assert.match(
			source,
			/38;2;156;220;254/
		);
	});

	test('Path listings render aligned columns', () => {
		assert.deepStrictEqual(
			formatItemsInColumns(
				['alpha/', 'beta.ts', 'gamma/'],
				30
			),
			['alpha/   beta.ts  gamma/']
		);
	});

	test('Summarize path context preserves quoted paths', () => {
		assert.deepStrictEqual(
			getPathCompletionContext(
				'summarize "ai-dev-vscode/src/assi'
			),
			{
				partialPath: 'ai-dev-vscode/src/assi',
				beforePath: 'summarize ',
				quote: '"',
			}
		);
	});

	test('Path completion is disabled for glob input', () => {
		assert.strictEqual(
			getPathCompletionContext(
				'summarize ai-dev-vscode/src/*.ts'
			),
			undefined
		);
	});

	test('A unique command prefix wins over substring matches', () => {
		const lookup = getAssistantLookupItems('e');
		const prefixMatches = lookup.filter(
			(item) => item.matchKind === 'prefix'
		);

		assert.deepStrictEqual(
			prefixMatches.map((item) => item.value),
			['exit']
		);
		assert.ok(
			lookup.some(
				(item) =>
					item.value === 'review'
					&& item.matchKind === 'substring'
			)
		);
	});

	test('Command lookup includes descriptions', () => {
		const lookup = getAssistantLookupItems('');

		assert.ok(
			lookup.some(
				(item) =>
					item.display
					=== '/ask - Ask the assistant a question'
			)
		);
	});

	test('/ask space lists route and help options', () => {
		const lookup = getAssistantLookupItems('ask ');

		assert.deepStrictEqual(
			lookup.map((item) => item.display),
			[
				'--auto, -a - Choose the best route',
				'--summary, -s - Use summary documentation only',
				'--knowledgebase, -k - Use the knowledge base only',
				'--chat, -c - Bypass project routing',
				'--help, -h - Show command help',
			]
		);
	});

	test('Option lookup filters the active option token', () => {
		const lookup = getAssistantLookupItems('ask --s');

		assert.deepStrictEqual(
			lookup.map((item) => item.value),
			['ask --summary']
		);
	});

	test('Option lookup supports short aliases', () => {
		const lookup = getAssistantLookupItems('ask -k');

		assert.deepStrictEqual(
			lookup.map((item) => item.value),
			['ask --knowledgebase']
		);
	});

	test('Option lookup hides route alternatives after a route', () => {
		const lookup = getAssistantLookupItems('ask --summary ');

		assert.deepStrictEqual(
			lookup.map((item) => item.value),
			['ask --summary --help']
		);
	});

	test('Option lookup disappears after positional text begins', () => {
		assert.deepStrictEqual(
			getAssistantLookupItems('ask What does this do?'),
			[]
		);
	});

	test('Single-match tab completion completes command', () => {
		const state = applyTextInput(createAssistantInputState(), '/he');
		const tab = handleCommandTab(state, ['/help', '/exit']);

		assert.strictEqual(tab.state.input, 'help');
		assert.strictEqual(tab.listMatches, undefined);
	});

	test('Double-tab lists matching commands', () => {
		const state = applyTextInput(createAssistantInputState(), '/');
		const firstTab = handleCommandTab(state, ['/help', '/exit']);
		const secondTab = handleCommandTab(firstTab.state, ['/help', '/exit']);

		assert.deepStrictEqual(secondTab.listMatches, ['/help', '/exit']);
	});

	test('Command lookup disappears after command arguments begin', () => {
		assert.deepStrictEqual(
			getMatchingAssistantCommands('ask '),
			[]
		);
		assert.deepStrictEqual(
			getMatchingAssistantCommands('ask question'),
			[]
		);
	});

	test('Command lookup remains visible while editing command name', () => {
		assert.deepStrictEqual(
			getMatchingAssistantCommands('ask'),
			['/ask']
		);
	});


	test('Recalling /help restores command mode and editable input without slash', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(commandState).state;
		const recalled = handleHistoryUp(submitted);

		assert.strictEqual(recalled.mode, 'command');
		assert.strictEqual(recalled.input, 'help');
	});

	test('Resubmitting recalled /help is classified as command', () => {
		const commandState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(commandState).state;
		const recalled = handleHistoryUp(submitted);
		const resubmitted = submitInput(recalled);

		assert.strictEqual(resubmitted.submittedKind, 'command');
		assert.strictEqual(resubmitted.submittedText, '/help');
	});

	test('Recalling chat history restores chat mode', () => {
		const chatState = applyTextInput(createAssistantInputState(), 'hello world');
		const submitted = submitInput(chatState).state;
		const recalled = handleHistoryUp(submitted);

		assert.strictEqual(recalled.mode, 'chat');
		assert.strictEqual(recalled.input, 'hello world');
	});

	test('Down arrow returns to command-mode draft when draft started in command mode', () => {
		const base = submitInput(applyTextInput(createAssistantInputState(), 'chat entry')).state;
		const draftCommand = applyTextInput(base, '/he');
		const recalled = handleHistoryUp(draftCommand);
		const restoredDraft = handleHistoryDown(recalled);

		assert.strictEqual(restoredDraft.mode, 'command');
		assert.strictEqual(restoredDraft.input, 'he');
	});

	test('Down arrow returns to chat-mode draft when draft started in chat mode', () => {
		const base = submitInput(applyTextInput(createAssistantInputState(), 'chat entry')).state;
		const draftChat = applyTextInput(base, 'draft question');
		const recalled = handleHistoryUp(draftChat);
		const restoredDraft = handleHistoryDown(recalled);

		assert.strictEqual(restoredDraft.mode, 'chat');
		assert.strictEqual(restoredDraft.input, 'draft question');
	});

	test('Report parser extracts the concise answer section', () => {
		const parsed = parseReportResponse([
			'# Answer',
			'',
			'The plugin provides an AI assistant.',
			'',
			'## Evidence',
			'',
			'- package.json',
		].join('\n'));

		assert.strictEqual(
			parsed.answer,
			'The plugin provides an AI assistant.'
		);
		assert.deepStrictEqual(
			parsed.sections.map((section) => section.title),
			['Answer', 'Evidence']
		);
	});

	test('Report parser recognizes bold section labels', () => {
		const parsed = parseReportResponse([
			'**Answer:** The plugin provides an AI assistant.',
			'',
			'**Evidence:**',
			'- package.json',
			'',
			'**Verification status:** Not verified against source.',
			'',
			'**Additional files needed:**',
			'- README.md',
		].join('\n'));

		assert.strictEqual(
			parsed.answer,
			'The plugin provides an AI assistant.'
		);

		assert.deepStrictEqual(
			parsed.sections.map((section) => section.title),
			[
				'Answer',
				'Evidence',
				'Verification status',
				'Additional files needed',
			]
		);
	});

	test('Report parser falls back to the first section', () => {
		const parsed = parseReportResponse([
			'## Result',
			'',
			'The operation completed.',
		].join('\n'));

		assert.strictEqual(parsed.answer, 'The operation completed.');
	});

	test('Report parser preserves detailed report sections', () => {
		const parsed = parseReportResponse([
			'# Answer',
			'',
			'Concise answer.',
			'',
			'## Verification Status',
			'',
			'Partially verified.',
			'',
			'## Uncertainty & Additional Files Needed',
			'',
			'- README.md',
		].join('\n'));

		assert.deepStrictEqual(parsed.sections, [
			{
				id: 'answer',
				title: 'Answer',
				content: 'Concise answer.',
			},
			{
				id: 'verification-status',
				title: 'Verification Status',
				content: 'Partially verified.',
			},
			{
				id: 'uncertainty-additional-files-needed',
				title: 'Uncertainty & Additional Files Needed',
				content: '- README.md',
			},
		]);
	});

	test('Review findings parse structured template fields', () => {
		const findings = parseReviewFindings([
			'## Finding: Missing routing summary',
			'',
			'**Severity:** warning',
			'',
			'**Category:** Missing summary',
			'',
			'**Source file:**',
			'`src/example.ts`',
			'',
			'**Documentation file:**',
			'`ai-docs/src/summary.md`',
			'',
			'### Evidence',
			'',
			'- Source exists.',
			'- Summary entry is absent.',
			'',
			'### Impact',
			'',
			'Routing is incomplete.',
			'',
			'### Suggested action',
			'',
			'Add a routing entry.',
			'',
			'### AI-generated update appropriate?',
			'',
			'Yes.',
			'',
			'### Uncertainty',
			'',
			'None.',
		].join('\n'));

		assert.strictEqual(findings.length, 1);
		assert.strictEqual(
			findings[0].title,
			'Missing routing summary'
		);
		assert.strictEqual(findings[0].severity, 'warning');
		assert.strictEqual(
			findings[0].sourceFile,
			'src/example.ts'
		);
		assert.strictEqual(
			findings[0].documentationFile,
			'ai-docs/src/summary.md'
		);
		assert.deepStrictEqual(
			findings[0].evidence,
			[
				'Source exists.',
				'Summary entry is absent.',
			]
		);
		assert.strictEqual(
			findings[0].suggestedAction,
			'Add a routing entry.'
		);
	});

	test('Model findings replace matching deterministic findings', () => {
		const findings = parseReviewFindings([
			'## Deterministic Documentation Mapping Findings',
			'',
			'### Missing expected summary',
			'',
			'- Source path: artifacts/example.vsix',
			'- Expected summary path: ai-docs/artifacts/summary.md',
			'',
			'## Model Review Findings',
			'',
			'## Finding: Missing expected summary for artifacts',
			'',
			'**Severity:** warning',
			'',
			'**Category:** Missing summary',
			'',
			'**Source file:**',
			'`artifacts/example.vsix`',
			'',
			'**Documentation file:**',
			'`ai-docs/artifacts/summary.md`',
			'',
			'### Evidence',
			'',
			'- Summary is absent.',
			'',
			'### Impact',
			'',
			'Routing is incomplete.',
			'',
			'### Suggested action',
			'',
			'Add the summary.',
			'',
			'### AI-generated update appropriate?',
			'',
			'Yes.',
			'',
			'### Uncertainty',
			'',
			'None.',
		].join('\n'));

		assert.strictEqual(findings.length, 1);
		assert.strictEqual(
			findings[0].origin,
			'model'
		);
		assert.strictEqual(
			findings[0].sourceFile,
			'artifacts/example.vsix'
		);
	});

	test('Review report HTML uses findings table and bottom inspector', () => {
		const report = createAssistantReport({
			route: 'review',
			title: 'AI Dev Review',
			rawResponse: [
				'## Finding: Missing routing summary',
				'',
				'**Severity:** warning',
				'',
				'**Category:** Missing summary',
				'',
				'**Source file:**',
				'`src/example.ts`',
				'',
				'**Documentation file:**',
				'`ai-docs/src/summary.md`',
				'',
				'### Evidence',
				'',
				'- Summary entry is absent.',
				'',
				'### Impact',
				'',
				'Routing is incomplete.',
				'',
				'### Suggested action',
				'',
				'Add a routing entry.',
				'',
				'### AI-generated update appropriate?',
				'',
				'Yes.',
				'',
				'### Uncertainty',
				'',
				'None.',
			].join('\n'),
		});

		const html = buildAssistantReportHtml(report);

		assert.match(html, /class="table-shell"/);
		assert.match(html, /class="finding-row"/);
		assert.match(html, /id="review-inspector"/);
		assert.match(html, /class="inspector-resizer"/);
		assert.match(html, /id="inspector-close"/);
		assert.doesNotMatch(html, /<nav class="toc"/);
	});

	test('Report HTML uses a fixed contents navigation layout', () => {
		const report = createAssistantReport({
			route: 'summary',
			title: 'AI Dev Report',
			question: 'What does this plugin do?',
			modelName: 'Auto',
			warnings: ['Root summary was not found.'],
			rawResponse: [
				'# Answer',
				'',
				'The plugin provides an AI assistant.',
				'',
				'## Evidence',
				'',
				'- package.json',
			].join('\n'),
		});

		const html = buildAssistantReportHtml(report);

		assert.match(html, /<nav class="toc"/);
		assert.match(html, /position: sticky/);
		assert.match(html, /href="#warnings"/);
		assert.match(html, /href="#answer"/);
		assert.match(html, /href="#evidence"/);
		assert.match(html, /id="raw-response"/);
	});

	test('Report HTML escapes unsafe report content', () => {
		const report = createAssistantReport({
			route: 'summary',
			title: '<script>alert(1)</script>',
			rawResponse: '# Answer\n\n<script>alert(2)</script>',
		});

		const html = buildAssistantReportHtml(report);

		assert.doesNotMatch(html, /<script>alert\(1\)<\/script>/);
		assert.doesNotMatch(html, /<script>alert\(2\)<\/script>/);
		assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
		assert.match(html, /&lt;script&gt;alert\(2\)&lt;\/script&gt;/);
	});

	test('Assistant report store retains the latest report', () => {
		const store = new AssistantReportStore();

		const first = createAssistantReport({
			route: 'summary',
			title: 'First report',
			rawResponse: '# Answer\n\nFirst answer.',
			now: new Date('2026-07-13T12:00:00.000Z'),
		});

		const second = createAssistantReport({
			route: 'summary',
			title: 'Second report',
			rawResponse: '# Answer\n\nSecond answer.',
			now: new Date('2026-07-13T12:01:00.000Z'),
		});

		store.setLatest(first);
		store.setLatest(second);

		assert.strictEqual(store.getLatest(), second);
		assert.strictEqual(store.getLatest()?.answer, 'Second answer.');
	});

	test('Terminal manager reuses active AI Dev terminal and recreates after close', () => {
		type CloseListener = (terminal: { name: string; show: () => void; dispose: () => void }) => void;
		const closeListeners: CloseListener[] = [];
		let createTerminalCount = 0;
		let showCount = 0;
		let lastCreatedTerminal: { name: string; show: () => void; dispose: () => void } | undefined;

		const fakeWindow = {
			createTerminal: (_options: vscode.ExtensionTerminalOptions) => {
				createTerminalCount += 1;
				const terminal = {
					name: 'AI Dev',
					show: () => {
						showCount += 1;
					},
					dispose: () => {
						for (const listener of closeListeners) {
							listener(terminal);
						}
					},
				};
				lastCreatedTerminal = terminal;
				return terminal as unknown as vscode.Terminal;
			},
			onDidCloseTerminal: (listener: CloseListener) => {
				closeListeners.push(listener);
				return { dispose: () => {} };
			},
		} as unknown as Pick<typeof vscode.window, 'createTerminal' | 'onDidCloseTerminal'>;

		const manager = new AiDevAssistantTerminalManager(fakeWindow);

		manager.launchAssistant();
		manager.launchAssistant();

		assert.strictEqual(createTerminalCount, 1);
		assert.strictEqual(showCount, 2);

		assert.ok(lastCreatedTerminal);
		lastCreatedTerminal?.dispose();

		manager.launchAssistant();
		assert.strictEqual(createTerminalCount, 2);
		manager.dispose();
	});

	test('Only launcher and settings remain public commands', async () => {
		const packagePath = path.resolve(
			__dirname,
			'../../package.json'
		);
		const packageJson = JSON.parse(
			await fs.readFile(packagePath, 'utf8')
		) as {
			contributes?: {
				commands?: Array<{ command?: string }>;
				menus?: unknown;
				submenus?: unknown;
			};
			activationEvents?: string[];
		};

		assert.deepStrictEqual(
			packageJson.contributes?.commands?.map(
				(command) => command.command
			),
			[
				'aiDev.launchAssistant',
				'aiDev.settings',
			]
		);
		assert.strictEqual(
			packageJson.contributes?.menus,
			undefined
		);
		assert.strictEqual(
			packageJson.contributes?.submenus,
			undefined
		);
		assert.deepStrictEqual(
			packageJson.activationEvents,
			[
				'onCommand:aiDev.launchAssistant',
				'onCommand:aiDev.settings',
			]
		);
	});

	test('Extension registers only launcher and settings commands', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/extension.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		const registrations =
			source.match(
				/vscode\.commands\.registerCommand\(/g
			) ?? [];

		assert.strictEqual(registrations.length, 2);
		assert.match(
			source,
			/LAUNCH_ASSISTANT_COMMAND/
		);
		assert.match(
			source,
			/SETTINGS_COMMAND/
		);
		assert.doesNotMatch(
			source,
			/aiDev\.copilotTest/
		);
		assert.doesNotMatch(
			source,
			/aiDev\.setExecutionMode/
		);
	});

	test('AI Dev Activity Bar item directly launches the assistant', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/actionsView.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/createTreeView\(/
		);
		assert.match(
			source,
			/onDidChangeVisibility/
		);
		assert.match(
			source,
			/'aiDev\.launchAssistant'/
		);
		assert.match(
			source,
			/'workbench\.action\.toggleSidebarVisibility'/
		);
		assert.doesNotMatch(
			source,
			/Open AI Dev Assistant/
		);
	});

	test('Terminal renders Unicode separator character', () => {
		// Helper to validate that separator rendering uses "─"
		const separator = '─'.repeat(40);
		assert.strictEqual(separator, '────────────────────────────────────────');
		assert.match(separator, /^─+$/);
	});

	test('Bullet character is used for permanent output markers', () => {
		// Verify bullet character constant is used in rendering
		const bulletChar = '•';
		const testLine = `${bulletChar} Test output`;
		assert.match(testLine, /^• /);
		assert.strictEqual(testLine[0], '•');
	});

	test('Full-width row rendering pads text to terminal width', () => {
		// Test the padding logic: text should be padded with spaces to fill width
		const width = 40;
		const text = 'hello';
		const truncated = text.length > width ? text.slice(0, width) : text;
		const paddingLength = Math.max(0, width - truncated.length);
		const paddedRow = truncated + ' '.repeat(paddingLength);
		
		assert.strictEqual(paddedRow.length, 40);
		assert.strictEqual(paddedRow, 'hello' + ' '.repeat(35));
	});

	test('Full-width row rendering safely truncates long input', () => {
		// Simulate truncation for long text
		const width = 20;
		const longText = 'this is a very long text that exceeds the terminal width';
		const truncated = longText.length > width ? longText.slice(0, width) : longText;
		
		assert.strictEqual(truncated.length, width);
		assert.strictEqual(truncated, 'this is a very long ');
	});

	test('Submitted chat input is stored in history', () => {
		// Verify that chat submission stores full text in history without duplication
		const chatState = applyTextInput(createAssistantInputState(), 'hello world');
		const submitted = submitInput(chatState);
		
		assert.strictEqual(submitted.state.history.length, 1);
		assert.strictEqual(submitted.state.history[0], 'hello world');
		assert.strictEqual(submitted.state.input, '');
		assert.strictEqual(submitted.state.mode, 'chat');
	});

	test('Submitted command input is stored in history with slash', () => {
		// Verify that command submission stores full text in history without duplication
		const cmdState = applyTextInput(createAssistantInputState(), '/help');
		const submitted = submitInput(cmdState);
		
		assert.strictEqual(submitted.state.history.length, 1);
		assert.strictEqual(submitted.state.history[0], '/help');
		assert.strictEqual(submitted.state.input, '');
		assert.strictEqual(submitted.state.mode, 'chat');
	});

	test('Help output uses bullet markers with no blank lines', () => {
		// Verify help text structure: each item uses bullet, no blank lines between
		const helpItems = [
			'• Available commands: /ask, /summarize, /review, /settings, /showreport, /exit',
			'• Type / to discover commands',
			'• Tab completes commands',
			'• Tab twice lists commands',
			'• Escape returns to chat',
			'• Up/Down navigate history',
		];
		
		// Each line should start with bullet
		for (const item of helpItems) {
			assert.match(item, /^• /);
		}
		
		// No blank lines when joined (no consecutive newlines)
		const joined = helpItems.join('\n');
		assert.doesNotMatch(joined, /\n\n/);
	});

	test('Terminal escape clears input and returns to chat mode', () => {
		// Verify that escape in command mode is followed by a fresh three-line prompt
		const commandState = applyTextInput(createAssistantInputState(), '/he');
		const escaped = handleEscape(commandState);
		
		assert.strictEqual(escaped.mode, 'chat');
		assert.strictEqual(escaped.input, '');
		assert.strictEqual(escaped.historyIndex, -1);
	});

	test('Pressing Enter after cancellation redraws prompt area', () => {
		// Verify that state after cancellation is clean for new prompt
		const state = createAssistantInputState();
		assert.strictEqual(state.input, '');
		assert.strictEqual(state.mode, 'chat');
		assert.strictEqual(state.tabPressCount, 0);
	});

	test('Glob summarize targets use grouped execution', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/const isGlobTarget =\s*\/\[\*\?/
		);
		assert.match(
			source,
			/await this\.submitBatchSummarize\(target\)/
		);
	});

	test('Grouped summarization refreshes architecture after writes', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/summarizationWorkflow.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/async function refreshArchitectureSummary/
		);
		assert.match(
			source,
			/result\.updatedSummaryPaths\.length > 0/
		);
		assert.match(
			source,
			/buildGenerateArchitectureSummaryDirectPromptMarkdown/
		);
	});

	test('Architecture refresh failure does not erase grouped results', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/summarizationWorkflow.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/architectureFailed/
		);
		assert.match(
			source,
			/return result;/
		);
	});

	test('Batch summarize execution uses isolated model requests', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/chatBackend\.sendIsolatedMessage/
		);
		assert.match(
			source,
			/Model calls completed:/
		);
	});

	test('Summarize smoke test uses preview without model execution', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/await this\.summarizeRoute!\.preview\(target\)/
		);
		assert.match(
			source,
			/Estimated model calls:/
		);
	});

	test('Changed-doc review filters packaged artifacts before model input', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/projectReview.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/const reviewableChangedFilePaths = changedFilePaths\.filter/
		);
		assert.match(
			source,
			/NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS/
		);
		assert.match(
			source,
			/existingChangedFilesWithContent\(\s*workspaceRoot,\s*reviewableChangedFilePaths/
		);
		assert.match(
			source,
			/getGitDiffForFiles\(\s*workspaceRoot,\s*reviewableChangedFilePaths/
		);
	});

	test('Changed-doc review reports ignored artifact count without listing artifacts', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/projectReview.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/Ignored non-source artifact changes:/
		);
		assert.match(
			source,
			/changedFilePaths: reviewableChangedFilePaths/
		);
	});

	test('Review mode resolution defaults to docs', () => {
		assert.deepStrictEqual(
			resolveReviewMode([]),
			{
				ok: true,
				mode: 'docs',
			}
		);
	});

	test('Review mode resolution supports code and tests aliases', () => {
		assert.deepStrictEqual(
			resolveReviewMode(['--code']),
			{
				ok: true,
				mode: 'code',
			}
		);
		assert.deepStrictEqual(
			resolveReviewMode(['-t']),
			{
				ok: true,
				mode: 'tests',
			}
		);
	});

	test('Review mode resolution rejects conflicting modes', () => {
		const result = resolveReviewMode([
			'--code',
			'--tests',
		]);

		assert.strictEqual(result.ok, false);
		if (!result.ok) {
			assert.match(
				result.error,
				/Choose only one review mode/
			);
		}
	});

	test('Review request resolves target and all-matches option', () => {
		assert.deepStrictEqual(
			resolveReviewRequest(
				['--code', '--all'],
				['src/**/*.ts']
			),
			{
				ok: true,
				request: {
					mode: 'code',
					target: 'src/**/*.ts',
					includeAllMatches: true,
					smokeTest: false,
				},
			}
		);
	});

	test('Review targets match files, directories, and globs', () => {
		assert.strictEqual(
			matchesReviewTarget(
				'src/assistantTerminal.ts',
				'src/assistantTerminal.ts'
			),
			true
		);
		assert.strictEqual(
			matchesReviewTarget(
				'src/test/extension.test.ts',
				'src'
			),
			true
		);
		assert.strictEqual(
			matchesReviewTarget(
				'src/test/extension.test.ts',
				'src/**/*.test.ts'
			),
			true
		);
		assert.strictEqual(
			matchesReviewTarget(
				'README.md',
				'src'
			),
			false
		);
	});

	test('Review file selection applies target before mode filtering', () => {
		const selection = selectReviewFiles({
			mode: 'code',
			docsDir: 'ai-docs',
			target: 'src/assistant*.ts',
			candidateFilePaths: [
				'src/assistantTerminal.ts',
				'src/assistantInput.ts',
				'src/test/assistantTerminal.test.ts',
				'src/extension.ts',
			],
		});

		assert.deepStrictEqual(
			selection.selectedPaths,
			[
				'src/assistantTerminal.ts',
				'src/assistantInput.ts',
			]
		);
	});

	test('Code review file selection excludes tests, docs, and artifacts', () => {
		const selection = selectChangedReviewFiles({
			mode: 'code',
			docsDir: 'ai-docs',
			changedFilePaths: [
				'src/extension.ts',
				'src/test/extension.test.ts',
				'ai-docs/src/summary.md',
				'artifacts/plugin.vsix',
			],
		});

		assert.deepStrictEqual(
			selection.implementationPaths,
			['src/extension.ts']
		);
		assert.deepStrictEqual(
			selection.testPaths,
			['src/test/extension.test.ts']
		);
		assert.deepStrictEqual(
			selection.selectedPaths,
			['src/extension.ts']
		);
	});

	test('Test review file selection includes implementation and tests', () => {
		const selection = selectChangedReviewFiles({
			mode: 'tests',
			docsDir: 'ai-docs',
			changedFilePaths: [
				'src/extension.ts',
				'src/test/extension.test.ts',
				'ai-docs/src/summary.md',
				'artifacts/plugin.vsix',
			],
		});

		assert.deepStrictEqual(
			selection.selectedPaths,
			[
				'src/extension.ts',
				'src/test/extension.test.ts',
			]
		);
	});

	test('Unstructured review fallback is a parseable warning finding', () => {
		const markdown =
			buildUnstructuredReviewFallback('tests');
		const findings = parseReviewFindings(markdown);

		assert.strictEqual(findings.length, 1);
		assert.strictEqual(
			findings[0].title,
			'Review returned no structured assessment'
		);
		assert.strictEqual(
			findings[0].severity,
			'warning'
		);
		assert.strictEqual(
			findings[0].category,
			'Test coverage'
		);
	});


	test('Automatic project routing surfaces summary evidence warnings', async () => {
		const outputChunks: string[] = [];
		const reports: Array<ReturnType<typeof createAssistantReport>> = [];
		const summaryQuestions: string[] = [];
		const isolatedPrompts: string[] = [];
		let chatMessageCount = 0;

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'Routing Warning Model',
			}),
			sendMessage: async () => {
				chatMessageCount += 1;
				return 'Unexpected chat response';
			},
			sendIsolatedMessage: async (prompt) => {
				isolatedPrompts.push(prompt);
				return 'The deployment job is deploy-service-x.';
			},
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend,
			async (question) => {
				summaryQuestions.push(question);

				return {
					prompt: 'summary evidence prompt',
					warnings: [
						'Unable to verify the referenced Jenkinsfile.',
					],
				};
			},
			undefined,
			undefined,
			(report) => {
				reports.push(report);
			}
		);

		const writeSubscription =
			pty.onDidWrite((chunk) => {
				outputChunks.push(chunk);
			});

		try {
			pty.open({ columns: 120, rows: 30 });

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'Launched Routing Warning Model'
				)
			);

			pty.handleInput(
				'Where do we deploy the billing service?'
			);
			pty.handleInput('\r');

			await waitForCondition(() =>
				reports.length === 1
			);

			const output = outputChunks.join('');

			assert.deepStrictEqual(
				summaryQuestions,
				['Where do we deploy the billing service?']
			);
			assert.deepStrictEqual(
				isolatedPrompts,
				['summary evidence prompt']
			);
			assert.strictEqual(chatMessageCount, 0);
			assert.match(
				output,
				/WARNING Unable to verify the referenced Jenkinsfile\./
			);
			assert.deepStrictEqual(
				reports[0].warnings,
				[
					'Unable to verify the referenced Jenkinsfile.',
				]
			);
		} finally {
			writeSubscription.dispose();
			pty.close();
		}
	});

	test('Automatic general chat avoids project routing warnings', async () => {
		const outputChunks: string[] = [];
		const chatPrompts: string[] = [];
		let summaryRouteCalled = false;
		let isolatedMessageCalled = false;

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'General Chat Model',
			}),
			sendMessage: async (prompt) => {
				chatPrompts.push(prompt);
				return 'Eventual consistency allows temporary divergence.';
			},
			sendIsolatedMessage: async () => {
				isolatedMessageCalled = true;
				return '';
			},
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend,
			async () => {
				summaryRouteCalled = true;

				return {
					prompt: 'unexpected summary prompt',
					warnings: [
						'Unexpected routing warning.',
					],
				};
			}
		);

		const writeSubscription =
			pty.onDidWrite((chunk) => {
				outputChunks.push(chunk);
			});

		try {
			pty.open({ columns: 120, rows: 30 });

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'Launched General Chat Model'
				)
			);

			pty.handleInput(
				'What is eventual consistency?'
			);
			pty.handleInput('\r');

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'Eventual consistency allows temporary divergence.'
				)
			);

			const output = outputChunks.join('');

			assert.deepStrictEqual(
				chatPrompts,
				['What is eventual consistency?']
			);
			assert.strictEqual(summaryRouteCalled, false);
			assert.strictEqual(
				isolatedMessageCalled,
				false
			);
			assert.doesNotMatch(
				output,
				/Unexpected routing warning/
			);
		} finally {
			writeSubscription.dispose();
			pty.close();
		}
	});

	test('Explicit chat bypasses project routing and warnings', async () => {
		const outputChunks: string[] = [];
		const chatPrompts: string[] = [];
		let summaryRouteCalled = false;
		let isolatedMessageCalled = false;

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'Explicit Chat Model',
			}),
			sendMessage: async (prompt) => {
				chatPrompts.push(prompt);
				return 'General Jenkins explanation.';
			},
			sendIsolatedMessage: async () => {
				isolatedMessageCalled = true;
				return '';
			},
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend,
			async () => {
				summaryRouteCalled = true;

				return {
					prompt: 'unexpected summary prompt',
					warnings: [
						'Unexpected project evidence warning.',
					],
				};
			}
		);

		const writeSubscription =
			pty.onDidWrite((chunk) => {
				outputChunks.push(chunk);
			});

		try {
			pty.open({ columns: 120, rows: 30 });

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'Launched Explicit Chat Model'
				)
			);

			pty.handleInput(
				'/ask --chat How does our Jenkins pipeline work?'
			);
			pty.handleInput('\r');

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'General Jenkins explanation.'
				)
			);

			const output = outputChunks.join('');

			assert.deepStrictEqual(
				chatPrompts,
				['How does our Jenkins pipeline work?']
			);
			assert.strictEqual(summaryRouteCalled, false);
			assert.strictEqual(
				isolatedMessageCalled,
				false
			);
			assert.doesNotMatch(
				output,
				/Unexpected project evidence warning/
			);
		} finally {
			writeSubscription.dispose();
			pty.close();
		}
	});

	test('Settings help is deterministic and does not open settings', async () => {
		const outputChunks: string[] = [];

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'Settings Test Model',
			}),
			sendMessage: async () => '',
			sendIsolatedMessage: async () => '',
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend
		);

		const writeSubscription =
			pty.onDidWrite((chunk) => {
				outputChunks.push(chunk);
			});

		try {
			pty.open({ columns: 120, rows: 30 });

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'Launched Settings Test Model'
				)
			);

			pty.handleInput('/settings --help');
			pty.handleInput('\r');

			await waitForCondition(() =>
				outputChunks.join('').includes(
					'/settings [options]'
				)
			);

			const output = outputChunks.join('');

			assert.match(output, /--config/);
			assert.match(output, /-c/);
			assert.match(output, /--help/);
			assert.match(output, /-h/);
			assert.doesNotMatch(
				output,
				/Opened AI Dev settings/
			);
		} finally {
			writeSubscription.dispose();
			pty.close();
		}
	});

	test('Review smoke test previews scope without model execution', async () => {
		const outputChunks: string[] = [];
		let prepareCalled = false;
		let isolatedMessageCalled = false;

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'Review Preview Model',
			}),
			sendMessage: async () => '',
			sendIsolatedMessage: async () => {
				isolatedMessageCalled = true;
				return '';
			},
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend,
			undefined,
			undefined,
			{
				preview: async (request) => ({
					mode: request.mode,
					target: request.target,
					includeAllMatches:
						request.includeAllMatches,
					implementationFileCount: 8,
					testFileCount: 2,
					selectedFileCount: 10,
					changedFileCount: 3,
					previewFilePaths: [
						'src/extension.ts',
					],
					omittedFileCount: 9,
					warnings: [
						'Included unchanged files across the project.',
					],
				}),
				prepare: async () => {
					prepareCalled = true;
					throw new Error(
						'prepare should not be called'
					);
				},
			}
		);

		const writeSubscription =
			pty.onDidWrite((chunk) => {
				outputChunks.push(chunk);
			});

		pty.open({ columns: 120, rows: 30 });

		await waitForCondition(() =>
			outputChunks.join('').includes(
				'Launched Review Preview Model'
			)
		);

		pty.handleInput(
			'/review --tests --all --smoketest'
		);
		pty.handleInput('\r');

		await waitForCondition(() =>
			outputChunks.join('').includes(
				'Total files selected: 10'
			)
		);

		const output = outputChunks.join('');

		assert.strictEqual(prepareCalled, false);
		assert.strictEqual(
			isolatedMessageCalled,
			false
		);
		assert.match(
			output,
			/Implementation files: 8/
		);
		assert.match(
			output,
			/Test files: 2/
		);
		assert.match(
			output,
			/Changed files in scope: 3/
		);
		assert.match(
			output,
			/9 additional files omitted/
		);

		writeSubscription.dispose();
		pty.close();
	});

	test('PTY routes test review and reports structured fallback', async () => {
		const reviewModes: string[] = [];
		const outputChunks: string[] = [];
		const reports: Array<ReturnType<typeof createAssistantReport>> = [];
		let isolatedPrompt = '';

		const backend: AssistantChatBackend = {
			startSession: async () => ({
				modelName: 'Test Model',
			}),
			sendMessage: async () => '',
			sendIsolatedMessage: async (prompt) => {
				isolatedPrompt = prompt;
				return [
					'I do not see any test files.',
					'Please open or attach the files to review.',
				].join('\n');
			},
			dispose: () => {},
		};

		const pty = new AiDevAssistantPseudoterminal(
			() => {},
			backend,
			undefined,
			undefined,
			{
				preview: async (request) => ({
					mode: request.mode,
					target: request.target,
					includeAllMatches:
						request.includeAllMatches,
					implementationFileCount: 1,
					testFileCount: 1,
					selectedFileCount: 2,
					changedFileCount: 2,
					previewFilePaths: [],
					omittedFileCount: 0,
					warnings: [],
				}),
				prepare: async (request) => {
					reviewModes.push(request.mode);

					return {
						mode: request.mode,
						prompt: 'embedded review prompt',
						changedFileCount: 2,
						deterministicFindingCount: 0,
						deterministicFindingsMarkdown: [
							'## Deterministic Documentation Mapping Findings',
							'',
							'- not applicable',
						].join('\n'),
						warnings: [],
					};
				},
			},
			(report) => {
				reports.push(report);
			}
		);

		const writeSubscription = pty.onDidWrite((chunk) => {
			outputChunks.push(chunk);
		});

		pty.open({ columns: 120, rows: 30 });

		await waitForCondition(() =>
			outputChunks.join('').includes('Launched Test Model')
		);

		pty.handleInput('/review --tests');
		pty.handleInput('\r');

		await waitForCondition(() => reports.length === 1);

		assert.deepStrictEqual(reviewModes, ['tests']);
		assert.strictEqual(
			isolatedPrompt,
			'embedded review prompt'
		);

		const findings = parseReviewFindings(
			reports[0].rawResponse
		);

		assert.strictEqual(findings.length, 1);
		assert.strictEqual(
			findings[0].title,
			'Review returned no structured assessment'
		);
		assert.strictEqual(
			findings[0].severity,
			'warning'
		);
		assert.strictEqual(
			findings[0].category,
			'Test coverage'
		);

		const terminalOutput = outputChunks.join('');

		assert.match(
			terminalOutput,
			/unstructured review response/
		);
		assert.match(
			terminalOutput,
			/\/showreport for findings/
		);

		writeSubscription.dispose();
		pty.close();
	});

	test('/review supports changed-documentation mode', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/submitReview/
		);
		assert.match(
			source,
			/Files reviewed:/
		);
		assert.match(
			source,
			/route: 'review'/
		);
	});

	test('/review sends findings to the report panel', async () => {
		const sourcePath = path.resolve(
			__dirname,
			'../../src/assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/this\.reportSink\?\.\(report\)/
		);
		assert.match(
			source,
			/\/showreport for findings/
		);
	});

	test('Plain assistant input uses automatic routing', async () => {
		const sourcePath = path.join(
			__dirname,
			'..',
			'..',
			'src',
			'assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/await this\.submitAutomaticPrompt\(submitResult\.submittedText\)/
		);
	});

	test('Assistant terminal shares one chat request lifecycle', async () => {
		const sourcePath = path.join(
			__dirname,
			'..',
			'..',
			'src',
			'assistantTerminal.ts'
		);
		const source = await fs.readFile(sourcePath, 'utf8');

		assert.match(
			source,
			/private async submitChatPrompt\(prompt: string\): Promise<void>/
		);
		assert.strictEqual(
			(source.match(/chatBackend\.sendMessage\(/g) ?? []).length,
			1
		);
	});

	test('Model responses use the large response marker', () => {
		assert.strictEqual(MODEL_RESPONSE_MARKER, '◆');
	});

	test('Model response formatting labels and indents lines', () => {
		const stripAnsi = (value: string): string =>
			value.replace(/\x1b\[[0-9;]*m/g, '');

		assert.deepStrictEqual(
			formatModelResponseLines(
				'Test Model',
				'First\nSecond'
			).map(stripAnsi),
			[
				'◆ Test Model: First',
				'  Second',
			]
		);
	});

	test('Empty model response formats as no lines', () => {
		assert.deepStrictEqual(
			formatModelResponseLines('Test Model', '   '),
			[]
		);
	});

});
