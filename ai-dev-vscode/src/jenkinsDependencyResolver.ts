import type {
	DependencyEdge,
} from './dependencyMap';
import {
	normalizePathForMarkdown,
} from './workspace';

export const JENKINS_PIPELINE_SCRIPT_EDGE_KIND =
	'jenkins-pipeline-script';

const CPS_SCM_FLOW_DEFINITION =
	'org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition';

function decodeXmlText(value: string): string {
	return value
		.replace(/&lt;/g, '<')
		.replace(/&gt;/g, '>')
		.replace(/&quot;/g, '"')
		.replace(/&apos;/g, '\'')
		.replace(/&amp;/g, '&');
}

function extractDefinitionTag(
	configXml: string
): string | undefined {
	const match = configXml.match(
		/<definition\b[^>]*>/i
	);

	return match?.[0];
}

function extractDefinitionClass(
	definitionTag: string
): string | undefined {
	const match = definitionTag.match(
		/\bclass\s*=\s*(?:"([^"]+)"|'([^']+)')/i
	);

	return match?.[1] ?? match?.[2];
}

export function extractJenkinsPipelineScriptPath(
	configXml: string
): string | undefined {
	const definitionTag =
		extractDefinitionTag(configXml);

	if (!definitionTag) {
		return undefined;
	}

	const definitionClass =
		extractDefinitionClass(definitionTag);

	if (definitionClass !== CPS_SCM_FLOW_DEFINITION) {
		return undefined;
	}

	const scriptPathMatch = configXml.match(
		/<scriptPath\b[^>]*>([\s\S]*?)<\/scriptPath>/i
	);

	if (!scriptPathMatch?.[1]) {
		return undefined;
	}

	const scriptPath = normalizePathForMarkdown(
		decodeXmlText(scriptPathMatch[1]).trim()
	)
		.replace(/^\.\/+/, '')
		.replace(/^\/+/, '');

	return scriptPath || undefined;
}

export function resolveJenkinsPipelineDependency(
	params: {
		configPath: string;
		configXml: string;
		candidatePaths: string[];
	}
): DependencyEdge | undefined {
	const sourcePath = normalizePathForMarkdown(
		params.configPath
	);
	const scriptPath =
		extractJenkinsPipelineScriptPath(
			params.configXml
		);

	if (!scriptPath) {
		return undefined;
	}

	const normalizedCandidates = [
		...new Set(
			params.candidatePaths.map((candidatePath) =>
				normalizePathForMarkdown(candidatePath)
					.replace(/^\.\/+/, '')
					.replace(/^\/+/, '')
			)
		),
	].sort((left, right) =>
		left.localeCompare(right)
	);

	const exactMatches = normalizedCandidates.filter(
		(candidatePath) =>
			candidatePath === scriptPath
	);

	if (exactMatches.length === 1) {
		return {
			sourcePath,
			targetPath: exactMatches[0],
			kind: JENKINS_PIPELINE_SCRIPT_EDGE_KIND,
			resolution: 'exact',
			evidence: [
				{
					kind: 'jenkins-script-path',
					detail:
						`SCM Pipeline definition references scriptPath "${scriptPath}", which exactly matches a workspace file.`,
					sourcePath,
				},
			],
		};
	}

	const suffix = `/${scriptPath}`;
	const suffixMatches = normalizedCandidates.filter(
		(candidatePath) =>
			candidatePath.endsWith(suffix)
	);

	if (suffixMatches.length === 1) {
		return {
			sourcePath,
			targetPath: suffixMatches[0],
			kind: JENKINS_PIPELINE_SCRIPT_EDGE_KIND,
			resolution: 'inferred',
			evidence: [
				{
					kind: 'jenkins-script-path-suffix',
					detail:
						`SCM Pipeline definition references scriptPath "${scriptPath}", which uniquely matches workspace suffix "${suffixMatches[0]}".`,
					sourcePath,
				},
			],
		};
	}

	if (suffixMatches.length > 1) {
		return {
			sourcePath,
			kind: JENKINS_PIPELINE_SCRIPT_EDGE_KIND,
			resolution: 'ambiguous',
			evidence: [
				{
					kind: 'jenkins-script-path-suffix',
					detail:
						`SCM Pipeline definition references scriptPath "${scriptPath}", which matches multiple workspace files: ${suffixMatches.join(', ')}.`,
					sourcePath,
				},
			],
		};
	}

	return {
		sourcePath,
		kind: JENKINS_PIPELINE_SCRIPT_EDGE_KIND,
		resolution: 'unresolved',
		evidence: [
			{
				kind: 'jenkins-script-path',
				detail:
					`SCM Pipeline definition references scriptPath "${scriptPath}", but no workspace file matched it.`,
				sourcePath,
			},
		],
	};
}
