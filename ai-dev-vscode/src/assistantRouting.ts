export type AutomaticAssistantRoute = 'chat' | 'summary';

const PROJECT_REFERENCE_PATTERNS = [
	/\bthis\s+(project|repo|repository|plugin|extension|workspace|codebase|application|app|service|system)\b/i,
	/\bthe\s+(project|repo|repository|plugin|extension|workspace|codebase)\b/i,
	/\bour\s+(project|repo|repository|plugin|extension|workspace|codebase|application|service|system)\b/i,
	/\bwhere\s+(do|does|is|are)\s+we\b/i,
	/\bhow\s+(do|does)\s+we\b/i,
	/\bin\s+this\s+(file|class|function|module|package|project|repo|repository)\b/i,
];

const PROJECT_OPERATION_PATTERNS = [
	/\b(jenkins|jenkinsfile|pipeline|deployment|deploy|build job|release job)\b/i,
	/\b(architecture|dependency map|routing documentation|summary documentation)\b/i,
	/\b(package\.json|extension\.ts|README\.md|\.ai-dev\.yaml)\b/i,
	/\b(src|lib|test|tests|docs|ai-docs)\//i,
	/\b[a-z0-9_-]+\.(ts|tsx|js|jsx|java|cs|py|go|rs|cpp|c|h|yaml|yml|json|md)\b/i,
];

export function chooseAutomaticAssistantRoute(
	question: string
): AutomaticAssistantRoute {
	const trimmed = question.trim();

	if (!trimmed) {
		return 'chat';
	}

	if (
		PROJECT_REFERENCE_PATTERNS.some((pattern) => pattern.test(trimmed))
		|| PROJECT_OPERATION_PATTERNS.some((pattern) => pattern.test(trimmed))
	) {
		return 'summary';
	}

	return 'chat';
}
