export interface WorkflowInstructionFile {
	label: string;
	relativePath: string;
}

export interface AiDevWorkflow {
	id: string;
	label: string;
	title: string;
	description: string;
	usage: string;
	buttonLabel: string;
	commandId: string;
	instructionFiles: WorkflowInstructionFile[];
}

export const AI_DEV_WORKFLOWS: AiDevWorkflow[] = [
	{
		id: 'generateUnitDoc',
		label: 'Generate Summary Doc',
		title: 'Generate Summary Doc',
		description: 'Generates instructions for summarizing the selected source file.',
		usage:
			'Open or right-click a source file, then run Generate Summary Doc. In prompt-only mode, paste the copied instructions into Copilot/Codex/Claude/etc. The AI should create or update the expected summary entry and any related summary routing entries required by the workflow.',
		buttonLabel: 'Generate Summary Doc',
		commandId: 'aiDev.generateUnitDocPromptForActiveFile',
		instructionFiles: [
			{ label: 'Workflow', relativePath: 'workflows/generate-docs/generate-unit-doc.md' },
			{ label: 'Template', relativePath: 'workflows/generate-docs/templates/unit-doc.md' },
		],
	},
	{
		id: 'generateArchitectureSummary',
		label: 'Generate Architecture Summary',
		title: 'Generate Architecture Summary',
		description: 'Generates a directory-level architecture summary routed to directory summary files.',
		usage:
			'Run Generate Architecture Summary to produce or update ai-docs/architecture-summary.md. The output must remain directory-level and route to summary.md files without listing source files.',
		buttonLabel: 'Generate Architecture Summary',
		commandId: 'aiDev.generateArchitectureSummary',
		instructionFiles: [
			{ label: 'Workflow', relativePath: 'workflows/generate-docs/generate-architecture-summary.md' },
			{ label: 'Template', relativePath: 'workflows/generate-docs/templates/architecture-summary.md' },
		],
	},
	{
		id: 'reviewChangedDocs',
		label: 'Review Summary Docs',
		title: 'Review Summary Docs',
		description: 'Generates review instructions for changed files and related documentation.',
		usage:
			'Run after code or docs have changed. In prompt-only mode, paste the copied instructions into Copilot/Codex/Claude/etc. The AI should report documentation gaps, stale docs, routing issues, or risky changes.',
		buttonLabel: 'Review Summary Docs',
		commandId: 'aiDev.reviewChangedDocsPrompt',
		instructionFiles: [
			{ label: 'Workflow', relativePath: 'workflows/review/review-documentation.md' },
			{ label: 'Template', relativePath: 'workflows/review/finding-template.md' },
		],
	},
	{
		id: 'reviewFileDocs',
		label: 'Review Summary Doc for Selected File',
		title: 'Review Summary Doc for Selected File',
		description:
			'Generates focused review instructions for the selected source file and its matching summary entry.',
		usage:
			'Open or right-click a source file, then run Review Summary Doc for Selected File. In prompt-only mode, paste the copied instructions into Copilot/Codex/Claude/etc. The AI should review the selected source file against its expected summary entry.',
		buttonLabel: 'Review Summary Doc for Selected File',
		commandId: 'aiDev.reviewFileDocsPrompt',
		instructionFiles: [
			{ label: 'Workflow', relativePath: 'workflows/review/review-documentation.md' },
			{ label: 'Template', relativePath: 'workflows/review/finding-template.md' },
		],
	},
	{
		id: 'answerFromDocs',
		label: 'Answer From Summary Docs',
		title: 'Answer From Summary Docs',
		description: 'Answers a question using the repository\'s AI documentation workflow.',
		usage:
			'Click the button and enter a question. In prompt-only mode, paste the copied instructions into Copilot/Codex/Claude/etc. The AI should use .ai-dev.yaml to find the documentation summary, then answer using routed AI docs and source as needed.',
		buttonLabel: 'Answer From Summary Docs',
		commandId: 'aiDev.answerFromAiDocsPrompt',
		instructionFiles: [
			{ label: 'Workflow', relativePath: 'workflows/answer-docs/answer-from-ai-docs.md' },
		],
	},
];

export function getWorkflowById(workflowId: string): AiDevWorkflow | undefined {
	return AI_DEV_WORKFLOWS.find((workflow) => workflow.id === workflowId);
}
