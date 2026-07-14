export type AssistantReportRoute =
	| 'auto'
	| 'summary'
	| 'knowledgebase'
	| 'chat'
	| 'summarize'
	| 'review';

export interface AssistantReportSection {
	id: string;
	title: string;
	content: string;
}

export interface AssistantReport {
	id: string;
	title: string;
	timestamp: string;
	route: AssistantReportRoute;
	question?: string;
	modelName?: string;
	answer: string;
	warnings: string[];
	sections: AssistantReportSection[];
	rawResponse: string;
}

export interface ParsedReportResponse {
	answer: string;
	sections: AssistantReportSection[];
}

export type ReviewFindingSeverity =
	| 'blocking'
	| 'warning'
	| 'info'
	| 'unknown';

export interface ReviewFinding {
	id: string;
	title: string;
	severity: ReviewFindingSeverity;
	category: string;
	sourceFile: string;
	documentationFile: string;
	evidence: string[];
	impact: string;
	suggestedAction: string;
	aiGeneratedUpdateAppropriate: string;
	uncertainty: string;
	origin: 'deterministic' | 'model';
}

function normalizeFindingSeverity(
	value: string
): ReviewFindingSeverity {
	switch (value.trim().toLowerCase()) {
		case 'blocking':
			return 'blocking';
		case 'warning':
			return 'warning';
		case 'info':
			return 'info';
		default:
			return 'unknown';
	}
}

function extractBoldField(
	content: string,
	label: string
): string {
	const pattern = new RegExp(
		`^\\*\\*${label}:\\*\\*\\s*(.*?)\\s*$`,
		'im'
	);
	return content.match(pattern)?.[1]?.trim() ?? '';
}

function extractPathField(
	content: string,
	label: string
): string {
	const pattern = new RegExp(
		`^\\*\\*${label}:\\*\\*\\s*\\n+\\s*\`?([^\\n\`]+)\`?`,
		'im'
	);
	return content.match(pattern)?.[1]?.trim() ?? '';
}

function extractFindingSection(
	content: string,
	title: string
): string {
	const pattern = new RegExp(
		`^###\\s+${title}\\s*$\\n([\\s\\S]*?)(?=^###\\s+|(?![\\s\\S]))`,
		'im'
	);
	return content.match(pattern)?.[1]?.trim() ?? '';
}

function extractEvidenceItems(content: string): string[] {
	return content
		.replace(/\r\n/g, '\n')
		.split('\n')
		.map((line) => line.trim())
		.filter((line) => /^[-*]\s+/.test(line))
		.map((line) => line.replace(/^[-*]\s+/, '').trim());
}

function createReviewFindingId(
	title: string,
	index: number
): string {
	const normalized = title
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');

	return normalized
		? `${normalized}-${index + 1}`
		: `finding-${index + 1}`;
}

function parseModelReviewFindings(
	rawResponse: string
): ReviewFinding[] {
	const normalized = rawResponse
		.replace(/\r\n/g, '\n');

	const headingPattern =
		/^##\s+Finding:\s*(.+?)\s*$/gm;
	const matches = [...normalized.matchAll(headingPattern)];

	return matches.map((match, index) => {
		const start = (match.index ?? 0) + match[0].length;
		const end =
			index + 1 < matches.length
				? matches[index + 1].index ?? normalized.length
				: normalized.length;

		const content = normalized.slice(start, end);
		const title = match[1].trim();

		return {
			id: createReviewFindingId(title, index),
			title,
			severity: normalizeFindingSeverity(
				extractBoldField(content, 'Severity')
			),
			category:
				extractBoldField(content, 'Category')
				|| 'Uncategorized',
			sourceFile:
				extractPathField(content, 'Source file')
				|| 'none',
			documentationFile:
				extractPathField(
					content,
					'Documentation file'
				) || 'none',
			evidence: extractEvidenceItems(
				extractFindingSection(content, 'Evidence')
			),
			impact:
				extractFindingSection(content, 'Impact'),
			suggestedAction:
				extractFindingSection(
					content,
					'Suggested action'
				),
			aiGeneratedUpdateAppropriate:
				extractFindingSection(
					content,
					'AI-generated update appropriate\\?'
				),
			uncertainty:
				extractFindingSection(
					content,
					'Uncertainty'
				),
			origin: 'model' as const,
		};
	});
}

function parseDeterministicReviewFindings(
	rawResponse: string
): ReviewFinding[] {
	const normalized = rawResponse
		.replace(/\r\n/g, '\n');

	const sectionMatch = normalized.match(
		/^## Deterministic Documentation Mapping Findings\s*$\n([\s\S]*?)(?=^##\s+Model Review Findings|(?![\s\S]))/m
	);

	if (!sectionMatch) {
		return [];
	}

	const section = sectionMatch[1];
	const headingPattern = /^###\s+(.+?)\s*$/gm;
	const matches = [...section.matchAll(headingPattern)];

	return matches.map((match, index) => {
		const start = (match.index ?? 0) + match[0].length;
		const end =
			index + 1 < matches.length
				? matches[index + 1].index ?? section.length
				: section.length;

		const content = section.slice(start, end);
		const details = extractEvidenceItems(content);
		const title = match[1].trim();

		const sourceFile =
			details
				.find((detail) =>
					detail.startsWith('Source path:')
				)
				?.slice('Source path:'.length)
				.trim()
			|| 'none';

		const documentationFile =
			details
				.find((detail) =>
					detail.startsWith(
						'Expected summary path:'
					)
				)
				?.slice('Expected summary path:'.length)
				.trim()
			|| 'none';

		const recommendation =
			details
				.find((detail) =>
					detail.startsWith('Recommendation:')
				)
				?.slice('Recommendation:'.length)
				.trim()
			|| '';

		return {
			id: createReviewFindingId(
				`deterministic-${title}`,
				index
			),
			title,
			severity: 'warning' as const,
			category:
				title.toLowerCase().includes('missing')
					? 'Missing summary'
					: 'Deterministic finding',
			sourceFile,
			documentationFile,
			evidence: details.filter(
				(detail) =>
					!detail.startsWith('Recommendation:')
			),
			impact: '',
			suggestedAction: recommendation,
			aiGeneratedUpdateAppropriate: '',
			uncertainty: '',
			origin: 'deterministic' as const,
		};
	});
}

function findingsReferToSameProblem(
	left: ReviewFinding,
	right: ReviewFinding
): boolean {
	const sameSource =
		left.sourceFile !== 'none'
		&& left.sourceFile === right.sourceFile;

	const sameDocumentation =
		left.documentationFile !== 'none'
		&& left.documentationFile
			=== right.documentationFile;

	return sameSource && sameDocumentation;
}

export function parseReviewFindings(
	rawResponse: string
): ReviewFinding[] {
	const modelFindings =
		parseModelReviewFindings(rawResponse);

	const deterministicFindings =
		parseDeterministicReviewFindings(rawResponse)
			.filter(
				(deterministic) =>
					!modelFindings.some((model) =>
						findingsReferToSameProblem(
							deterministic,
							model
						)
					)
			);

	const severityRank:
		Record<ReviewFindingSeverity, number> = {
			blocking: 0,
			warning: 1,
			info: 2,
			unknown: 3,
		};

	return [
		...modelFindings,
		...deterministicFindings,
	].sort((left, right) => {
		const severityDifference =
			severityRank[left.severity]
			- severityRank[right.severity];

		if (severityDifference !== 0) {
			return severityDifference;
		}

		return left.title.localeCompare(right.title);
	});
}

function createSectionId(title: string, index: number): string {
	const normalized = title
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');

	return normalized || `section-${index + 1}`;
}

export function parseReportResponse(
	rawResponse: string
): ParsedReportResponse {
	const normalized = rawResponse
		.replace(/\r\n/g, '\n')
		.trim();

	if (!normalized) {
		return {
			answer: '',
			sections: [],
		};
	}

	const lines = normalized.split('\n');
	const sections: AssistantReportSection[] = [];
	let currentTitle = 'Answer';
	let currentLines: string[] = [];

	const pushSection = (): void => {
		const content = currentLines.join('\n').trim();
		if (!content) {
			currentLines = [];
			return;
		}

		sections.push({
			id: createSectionId(currentTitle, sections.length),
			title: currentTitle,
			content,
		});
		currentLines = [];
	};

	for (const line of lines) {
		const headingMatch = line.match(/^#{1,6}\s+(.+?)\s*$/);
		const boldLabelMatch = line.match(
			/^\*\*([^*]+?):\*\*\s*(.*)$/
		);

		if (headingMatch) {
			pushSection();
			currentTitle = headingMatch[1].trim();
			continue;
		}

		if (boldLabelMatch) {
			pushSection();
			currentTitle = boldLabelMatch[1].trim();

			const inlineContent = boldLabelMatch[2].trim();
			if (inlineContent) {
				currentLines.push(inlineContent);
			}

			continue;
		}

		currentLines.push(line);
	}

	pushSection();

	const answerSection = sections.find(
		(section) => section.title.toLowerCase() === 'answer'
	);

	return {
		answer: answerSection?.content ?? sections[0]?.content ?? normalized,
		sections,
	};
}

export function createAssistantReport(params: {
	route: AssistantReportRoute;
	title: string;
	question?: string;
	modelName?: string;
	warnings?: string[];
	rawResponse: string;
	now?: Date;
}): AssistantReport {
	const now = params.now ?? new Date();
	const parsed = parseReportResponse(params.rawResponse);

	return {
		id: now.toISOString(),
		title: params.title,
		timestamp: now.toISOString(),
		route: params.route,
		question: params.question,
		modelName: params.modelName,
		answer: parsed.answer,
		warnings: [...(params.warnings ?? [])],
		sections: parsed.sections,
		rawResponse: params.rawResponse,
	};
}

export class AssistantReportStore {
	private latestReport: AssistantReport | undefined;

	setLatest(report: AssistantReport): void {
		this.latestReport = report;
	}

	getLatest(): AssistantReport | undefined {
		return this.latestReport;
	}

	clear(): void {
		this.latestReport = undefined;
	}
}
