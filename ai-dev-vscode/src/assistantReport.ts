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
