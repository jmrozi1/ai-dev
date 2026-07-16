import * as path from 'node:path';
import type {
	DependencyEdge,
} from './dependencyMap';
import {
	normalizePathForMarkdown,
} from './workspace';

export const SHELL_COMMAND_SCRIPT_EDGE_KIND =
	'shell-command-script';

export const SHELL_SOURCE_EDGE_KIND =
	'shell-source';

interface LiteralFileReference {
	rawPath: string;
	kind:
		| typeof SHELL_COMMAND_SCRIPT_EDGE_KIND
		| typeof SHELL_SOURCE_EDGE_KIND;
	evidenceKind: string;
}

function normalizeReferencePath(
	rawPath: string
): string | undefined {
	const trimmed = rawPath.trim();

	if (
		!trimmed
		|| /\s/.test(trimmed)
		|| trimmed.includes('$')
		|| trimmed.includes('`')
		|| trimmed.startsWith('-')
		|| /^[a-z][a-z0-9+.-]*:/i.test(trimmed)
	) {
		return undefined;
	}

	const normalized =
		normalizePathForMarkdown(trimmed)
			.replace(/^\.\/+/, '')
			.replace(/^\/+/, '');

	return normalized || undefined;
}

function extractJenkinsShellReferences(
	contents: string
): LiteralFileReference[] {
	const references: LiteralFileReference[] = [];
	const regex =
		/\bsh\s*(?:\(\s*)?(['"])([^'"]+)\1\s*\)?/g;

	let match: RegExpExecArray | null;

	while ((match = regex.exec(contents)) !== null) {
		const rawPath =
			normalizeReferencePath(match[2] ?? '');

		if (!rawPath) {
			continue;
		}

		references.push({
			rawPath,
			kind: SHELL_COMMAND_SCRIPT_EDGE_KIND,
			evidenceKind: 'jenkins-sh-command',
		});
	}

	return references;
}

function extractShellReferences(
	contents: string
): LiteralFileReference[] {
	const references: LiteralFileReference[] = [];

	for (const line of contents.split(/\r?\n/)) {
		const sourceMatch = line.match(
			/^\s*(?:source|\.)\s+(['"]?)([^'"\s;]+)\1(?:\s|;|$)/
		);

		if (sourceMatch?.[2]) {
			const rawPath =
				normalizeReferencePath(sourceMatch[2]);

			if (rawPath) {
				references.push({
					rawPath,
					kind: SHELL_SOURCE_EDGE_KIND,
					evidenceKind: 'shell-source-command',
				});
			}
		}

		const execMatch = line.match(
			/^\s*exec\s+(['"]?)([^'"\s;]+)\1(?:\s|;|$)/
		);

		if (execMatch?.[2]) {
			const rawPath =
				normalizeReferencePath(execMatch[2]);

			if (rawPath) {
				references.push({
					rawPath,
					kind:
						SHELL_COMMAND_SCRIPT_EDGE_KIND,
					evidenceKind: 'shell-exec-command',
				});
			}
		}
	}

	return references;
}

function extractLiteralFileReferences(
	sourcePath: string,
	contents: string
): LiteralFileReference[] {
	const normalizedSourcePath =
		normalizePathForMarkdown(sourcePath);
	const lowerPath =
		normalizedSourcePath.toLowerCase();

	const references: LiteralFileReference[] = [];

	if (
		lowerPath.endsWith('.jenkins')
		|| path.posix.basename(lowerPath)
			=== 'jenkinsfile'
		|| lowerPath.endsWith('.groovy')
	) {
		references.push(
			...extractJenkinsShellReferences(contents)
		);
	}

	if (
		lowerPath.endsWith('.sh')
		|| contents.startsWith('#!/usr/bin/env bash')
		|| contents.startsWith('#!/bin/bash')
		|| contents.startsWith('#!/bin/sh')
	) {
		references.push(
			...extractShellReferences(contents)
		);
	}

	const seen = new Set<string>();

	return references.filter((reference) => {
		const key = [
			reference.kind,
			reference.rawPath,
		].join('\0');

		if (seen.has(key)) {
			return false;
		}

		seen.add(key);
		return true;
	});
}

function resolveLiteralReference(params: {
	sourcePath: string;
	reference: LiteralFileReference;
	candidatePaths: string[];
}): DependencyEdge {
	const sourcePath =
		normalizePathForMarkdown(params.sourcePath);
	const referencePath =
		params.reference.rawPath;
	const candidates = [
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

	const exactMatches = candidates.filter(
		(candidatePath) =>
			candidatePath === referencePath
	);

	if (exactMatches.length === 1) {
		return {
			sourcePath,
			targetPath: exactMatches[0],
			kind: params.reference.kind,
			resolution: 'exact',
			evidence: [
				{
					kind:
						params.reference.evidenceKind,
					detail:
						`Literal file reference "${referencePath}" exactly matches a workspace file.`,
					sourcePath,
				},
			],
		};
	}

	const sourceRelativePath =
		normalizePathForMarkdown(
			path.posix.normalize(
				path.posix.join(
					path.posix.dirname(sourcePath),
					referencePath
				)
			)
		);

	const relativeMatches = candidates.filter(
		(candidatePath) =>
			candidatePath === sourceRelativePath
	);

	if (relativeMatches.length === 1) {
		return {
			sourcePath,
			targetPath: relativeMatches[0],
			kind: params.reference.kind,
			resolution: 'exact',
			evidence: [
				{
					kind:
						params.reference.evidenceKind,
					detail:
						`Literal file reference "${referencePath}" resolves relative to ${sourcePath}.`,
					sourcePath,
				},
			],
		};
	}

	const suffix = `/${referencePath}`;
	const suffixMatches = candidates.filter(
		(candidatePath) =>
			candidatePath.endsWith(suffix)
	);

	if (suffixMatches.length === 1) {
		return {
			sourcePath,
			targetPath: suffixMatches[0],
			kind: params.reference.kind,
			resolution: 'inferred',
			evidence: [
				{
					kind:
						`${params.reference.evidenceKind}-suffix`,
					detail:
						`Literal file reference "${referencePath}" uniquely matches workspace suffix "${suffixMatches[0]}".`,
					sourcePath,
				},
			],
		};
	}

	if (suffixMatches.length > 1) {
		return {
			sourcePath,
			kind: params.reference.kind,
			resolution: 'ambiguous',
			evidence: [
				{
					kind:
						`${params.reference.evidenceKind}-suffix`,
					detail:
						`Literal file reference "${referencePath}" matches multiple workspace files: ${suffixMatches.join(', ')}.`,
					sourcePath,
				},
			],
		};
	}

	return {
		sourcePath,
		kind: params.reference.kind,
		resolution: 'unresolved',
		evidence: [
			{
				kind:
					params.reference.evidenceKind,
				detail:
					`Literal file reference "${referencePath}" did not match a workspace file.`,
				sourcePath,
			},
		],
	};
}

export function resolveDelegatedFileDependencies(
	params: {
		sourcePath: string;
		sourceContents: string;
		candidatePaths: string[];
	}
): DependencyEdge[] {
	return extractLiteralFileReferences(
		params.sourcePath,
		params.sourceContents
	).map((reference) =>
		resolveLiteralReference({
			sourcePath: params.sourcePath,
			reference,
			candidatePaths: params.candidatePaths,
		})
	);
}
