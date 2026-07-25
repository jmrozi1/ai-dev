import {
	normalizePathForMarkdown,
} from './workspace';
import * as ts from 'typescript';

export type SourceEvidenceRole =
	| 'primary-source'
	| 'dependency';

export type SourceEvidenceAuthority =
	| 'implementation'
	| 'test'
	| 'generated-vendor'
	| 'documentation-config';

export type SourceEvidenceMethod =
	| 'ts-declaration-span'
	| 'text-line-window'
	| 'whole-file-prefix-fallback';

export type SourceEvidenceSymbolAssociation =
	| 'explicit'
	| 'inferred';

export interface SourceEvidenceTarget {
	sourcePath: string;
	role: SourceEvidenceRole;
	authority: SourceEvidenceAuthority;
	primarySourcePath?: string;
	relationshipKind?: string;
	resolution?: 'exact' | 'inferred';
	symbol?: string;
	symbolAssociation?: SourceEvidenceSymbolAssociation;
	matchedTerms: string[];
	targetRelevanceScore?: number;
}

export interface SourceEvidenceCandidate {
	sourcePath: string;
	role: SourceEvidenceRole;
	authority: SourceEvidenceAuthority;
	method: SourceEvidenceMethod;
	startLine: number;
	endLine: number;
	symbol?: string;
	contents: string;
	score: number;
	primarySourcePath?: string;
	relationshipKind?: string;
	resolution?: 'exact' | 'inferred';
}

export interface AllocatedSourceEvidenceChunk
	extends SourceEvidenceCandidate {
	includedChars: number;
	clippedByBudget: boolean;
}

interface WindowRange {
	startLine: number;
	endLine: number;
	distinctTerms: Set<string>;
	termMatches: Map<string, number>;
}

interface SourceEvidenceWindowOptions {
	windowLinesBefore: number;
	windowLinesAfter: number;
	maxMatchedWindowsPerFile: number;
	wholeFilePrefixMaxChars: number;
}

interface AllocateSourceEvidenceOptions {
	totalBudgetChars: number;
	perFileCeilingChars: number;
	perChunkCapChars: number;
}

export const SOURCE_EVIDENCE_WINDOW_LINES_BEFORE = 16;
export const SOURCE_EVIDENCE_WINDOW_LINES_AFTER = 24;
export const SOURCE_EVIDENCE_MAX_MATCHED_WINDOWS_PER_FILE = 3;
export const SOURCE_EVIDENCE_WHOLE_FILE_PREFIX_MAX_CHARS = 12000;
export const SOURCE_EVIDENCE_MAX_MATCHES_PER_TERM_PER_WINDOW = 4;
export const SOURCE_EVIDENCE_MAX_MATCH_SCORE_TOTAL = 24;

interface DeclarationEntry {
	name: string;
	normalizedName: string;
	qualifiedName?: string;
	normalizedQualifiedName?: string;
	node: ts.Node;
	isExported: boolean;
	isTopLevel: boolean;
	isMember: boolean;
	index: number;
}

function normalizeSymbol(value: string): string {
	return value.toLowerCase().replace(/[^a-z0-9_$]/g, '');
}

function isTsJsSourcePath(sourcePath: string): boolean {
	return /\.(ts|tsx|js|jsx|mts|cts|mjs|cjs)$/i.test(sourcePath);
}

function scriptKindForSourcePath(sourcePath: string): ts.ScriptKind {
	const lowerPath = sourcePath.toLowerCase();

	if (lowerPath.endsWith('.tsx')) {
		return ts.ScriptKind.TSX;
	}

	if (lowerPath.endsWith('.jsx')) {
		return ts.ScriptKind.JSX;
	}

	if (
		lowerPath.endsWith('.js')
		|| lowerPath.endsWith('.mjs')
		|| lowerPath.endsWith('.cjs')
	) {
		return ts.ScriptKind.JS;
	}

	return ts.ScriptKind.TS;
}

function getPropertyNameText(
	name: ts.PropertyName | ts.BindingName | undefined
): string | undefined {
	if (!name) {
		return undefined;
	}

	if (ts.isIdentifier(name)) {
		return name.text;
	}

	if (ts.isStringLiteral(name) || ts.isNumericLiteral(name)) {
		return name.text;
	}

	if (ts.isComputedPropertyName(name)) {
		if (ts.isIdentifier(name.expression)) {
			return name.expression.text;
		}
	}

	return undefined;
}

function isNodeExported(node: ts.Node): boolean {
	const flags = ts.getCombinedModifierFlags(node as ts.Declaration);
	return (flags & ts.ModifierFlags.Export) !== 0;
}

function addDeclarationEntry(params: {
	entries: DeclarationEntry[];
	node: ts.Node;
	name?: string;
	qualifiedName?: string;
	isExported: boolean;
	isTopLevel: boolean;
	isMember: boolean;
}): void {
	if (!params.name) {
		return;
	}

	const trimmedName = params.name.trim();
	if (!trimmedName) {
		return;
	}

	params.entries.push({
		name: trimmedName,
		normalizedName: normalizeSymbol(trimmedName),
		qualifiedName: params.qualifiedName,
		normalizedQualifiedName: params.qualifiedName
			? normalizeSymbol(params.qualifiedName)
			: undefined,
		node: params.node,
		isExported: params.isExported,
		isTopLevel: params.isTopLevel,
		isMember: params.isMember,
		index: params.entries.length,
	});
}

function collectDeclarationEntries(
	sourceFile: ts.SourceFile
): DeclarationEntry[] {
	const entries: DeclarationEntry[] = [];

	for (const statement of sourceFile.statements) {
		const topLevelExported = isNodeExported(statement);

		if (
			ts.isFunctionDeclaration(statement)
			|| ts.isClassDeclaration(statement)
			|| ts.isInterfaceDeclaration(statement)
			|| ts.isTypeAliasDeclaration(statement)
			|| ts.isEnumDeclaration(statement)
		) {
			addDeclarationEntry({
				entries,
				node: statement,
				name: statement.name?.text,
				isExported: topLevelExported,
				isTopLevel: true,
				isMember: false,
			});
		}

		if (ts.isVariableStatement(statement)) {
			for (const declaration of statement.declarationList.declarations) {
				const declarationName = getPropertyNameText(declaration.name);
				addDeclarationEntry({
					entries,
					node: declaration,
					name: declarationName,
					isExported: topLevelExported,
					isTopLevel: true,
					isMember: false,
				});
			}
		}

		if (ts.isClassDeclaration(statement)) {
			const className = statement.name?.text;
			if (!className) {
				continue;
			}

			for (const member of statement.members) {
				if (
					ts.isMethodDeclaration(member)
					|| ts.isPropertyDeclaration(member)
				) {
					const memberName = getPropertyNameText(member.name);
					if (!memberName) {
						continue;
					}

					addDeclarationEntry({
						entries,
						node: member,
						name: memberName,
						qualifiedName: `${className}.${memberName}`,
						isExported: topLevelExported || isNodeExported(member),
						isTopLevel: false,
						isMember: true,
					});
				}
			}
		}

		if (ts.isExportAssignment(statement)) {
			if (ts.isIdentifier(statement.expression)) {
				addDeclarationEntry({
					entries,
					node: statement,
					name: statement.expression.text,
					isExported: true,
					isTopLevel: true,
					isMember: false,
				});
			}
		}
	}

	return entries;
}

function declarationEntryMatchRank(params: {
	entry: DeclarationEntry;
	requestedSymbol: string;
}): number {
	const requestedSymbol = params.requestedSymbol.trim();
	const normalizedRequested = normalizeSymbol(requestedSymbol);

	if (!requestedSymbol || !normalizedRequested) {
		return -1;
	}

	if (params.entry.qualifiedName === requestedSymbol) {
		return 120;
	}

	if (params.entry.name === requestedSymbol) {
		return 110;
	}

	if (params.entry.normalizedQualifiedName === normalizedRequested) {
		return 100;
	}

	if (params.entry.normalizedName === normalizedRequested) {
		return 90;
	}

	if (
		requestedSymbol.includes('.')
		&& params.entry.qualifiedName
		&& requestedSymbol.endsWith(`.${params.entry.name}`)
	) {
		return 80;
	}

	return -1;
}

function resolveDeclarationEntry(params: {
	sourceFile: ts.SourceFile;
	symbol: string;
}): DeclarationEntry | undefined {
	const entries = collectDeclarationEntries(params.sourceFile);
	if (entries.length === 0) {
		return undefined;
	}

	let bestEntry: DeclarationEntry | undefined;
	let bestRank = -1;

	for (const entry of entries) {
		const matchRank = declarationEntryMatchRank({
			entry,
			requestedSymbol: params.symbol,
		});

		if (matchRank < 0) {
			continue;
		}

		const exportBonus = entry.isExported ? 3 : 0;
		const topLevelBonus = entry.isTopLevel ? 2 : 0;
		const memberBonus = entry.isMember ? 1 : 0;
		const rank = matchRank + exportBonus + topLevelBonus + memberBonus;

		if (rank > bestRank) {
			bestRank = rank;
			bestEntry = entry;
			continue;
		}

		if (rank === bestRank && bestEntry) {
			if (entry.index < bestEntry.index) {
				bestEntry = entry;
			}
		}
	}

	return bestEntry;
}

const QUESTION_TERM_STOP_WORDS = new Set([
	'a',
	'an',
	'and',
	'are',
	'as',
	'at',
	'be',
	'by',
	'for',
	'from',
	'how',
	'in',
	'is',
	'it',
	'of',
	'on',
	'or',
	'that',
	'the',
	'this',
	'to',
	'was',
	'what',
	'when',
	'where',
	'which',
	'who',
	'why',
	'with',
]);

function escapeRegExp(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function countTermMatches(
	text: string,
	term: string
): number {
	if (!text || !term) {
		return 0;
	}

	const regex = new RegExp(escapeRegExp(term), 'gi');
	const matches = text.match(regex);

	return matches?.length ?? 0;
}

function countTokenOverlap(
	text: string,
	questionTerms: string[]
): number {
	if (!text || questionTerms.length === 0) {
		return 0;
	}

	const textTerms = new Set(
		tokenizeSourceEvidenceTextTerms(text)
	);
	let overlapCount = 0;

	for (const term of questionTerms) {
		if (textTerms.has(term)) {
			overlapCount += 1;
		}
	}

	return overlapCount;
}

function countBoundedTermOccurrences(
	text: string,
	questionTerms: string[],
	perTermCap: number
): number {
	if (!text || questionTerms.length === 0) {
		return 0;
	}

	const lowerText = text.toLowerCase();
	let total = 0;

	for (const term of questionTerms) {
		let count = 0;
		let startIndex = 0;

		while (count < perTermCap) {
			const index = lowerText.indexOf(term, startIndex);
			if (index === -1) {
				break;
			}

			count += 1;
			startIndex = index + term.length;
		}

		total += count;
	}

	return total;
}

export function tokenizeSourceEvidenceTextTerms(
	text: string
): string[] {
	const normalized = text
		.replace(/([a-z0-9])([A-Z])/g, '$1 $2')
		.replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
		.toLowerCase()
		.replace(/[\\/._:-]+/g, ' ')
		.replace(/[^a-z0-9\s]+/g, ' ');

	const terms = normalized
		.split(/\s+/)
		.map((term) => term.trim())
		.filter((term) => term.length >= 3)
		.filter((term) => !QUESTION_TERM_STOP_WORDS.has(term));

	return [...new Set(terms)];
}

export function tokenizeSourceEvidenceQuestionTerms(
	question: string
): string[] {
	return tokenizeSourceEvidenceTextTerms(question);
}

export function classifySourceEvidenceAuthority(
	sourcePath: string
): SourceEvidenceAuthority {
	const normalized = normalizePathForMarkdown(sourcePath).toLowerCase();

	if (
		normalized.includes('/vendor/')
		|| normalized.startsWith('vendor/')
		|| normalized.includes('/node_modules/')
		|| normalized.startsWith('node_modules/')
		|| normalized.includes('/dist/')
		|| normalized.startsWith('dist/')
		|| normalized.includes('/out/')
		|| normalized.startsWith('out/')
		|| normalized.includes('/artifacts/')
		|| normalized.startsWith('artifacts/')
	) {
		return 'generated-vendor';
	}

	if (
		normalized.includes('/src/test/')
		|| normalized.startsWith('src/test/')
		|| normalized.includes('/test/')
		|| normalized.startsWith('test/')
		|| normalized.includes('/tests/')
		|| normalized.startsWith('tests/')
		|| normalized.includes('/__tests__/')
		|| /\.(test|spec)\.[a-z0-9]+$/.test(normalized)
	) {
		return 'test';
	}

	if (/\.(md|mdx|txt|rst|adoc|ya?ml|json|toml|ini|cfg|conf|xml)$/.test(normalized)) {
		return 'documentation-config';
	}

	return 'implementation';
}

function mergeWindowRanges(
	windows: WindowRange[]
): WindowRange[] {
	if (windows.length === 0) {
		return [];
	}

	const sorted = [...windows].sort((left, right) => {
		if (left.startLine !== right.startLine) {
			return left.startLine - right.startLine;
		}

		return left.endLine - right.endLine;
	});
	const merged: WindowRange[] = [];

	for (const windowRange of sorted) {
		const previous = merged[merged.length - 1];

		if (
			previous
			&& windowRange.startLine <= previous.endLine + 1
		) {
			previous.endLine = Math.max(
				previous.endLine,
				windowRange.endLine
			);
			for (const term of windowRange.distinctTerms) {
				previous.distinctTerms.add(term);
			}
			for (const [term, count] of windowRange.termMatches.entries()) {
				const existingCount = previous.termMatches.get(term) ?? 0;
				previous.termMatches.set(
					term,
					Math.min(
						SOURCE_EVIDENCE_MAX_MATCHES_PER_TERM_PER_WINDOW,
						existingCount + count
					)
				);
			}
			continue;
		}

		merged.push({
			startLine: windowRange.startLine,
			endLine: windowRange.endLine,
			distinctTerms: new Set(windowRange.distinctTerms),
			termMatches: new Map(windowRange.termMatches),
		});
	}

	return merged;
}

function scoreSourceEvidenceCandidate(params: {
	role: SourceEvidenceRole;
	authority: SourceEvidenceAuthority;
	resolution?: 'exact' | 'inferred';
	method: SourceEvidenceMethod;
	distinctTermMatches: number;
	totalTermMatches: number;
	symbolTermMatches?: number;
	symbolAssociation?: SourceEvidenceSymbolAssociation;
	resolvedDeclarationSymbol?: boolean;
	targetRelevanceScore?: number;
}): number {
	let score = 0;
	const boundedTotalTermMatches = Math.min(
		params.totalTermMatches,
		SOURCE_EVIDENCE_MAX_MATCH_SCORE_TOTAL
	);

	score += params.distinctTermMatches * 160;
	score += boundedTotalTermMatches * 8;
	score += params.role === 'primary-source' ? 20 : 0;
	score += params.authority === 'implementation' ? 120 : 0;
	score += params.authority === 'documentation-config' ? 20 : 0;
	score += params.authority === 'test' ? -80 : 0;
	score += params.authority === 'generated-vendor' ? -120 : 0;
	score += params.resolution === 'exact' ? 6 : 0;
	score += params.resolution === 'inferred' ? 3 : 0;
	score += (params.symbolTermMatches ?? 0) * 90;
	score += params.symbolAssociation === 'explicit' ? 140 : 0;
	score += params.symbolAssociation === 'inferred' ? 70 : 0;
	score += params.resolvedDeclarationSymbol ? 220 : 0;
	score += params.method === 'ts-declaration-span' ? 200 : 0;
	score += params.method === 'text-line-window' ? 8 : -5;
	score += Math.max(0, params.targetRelevanceScore ?? 0) * 20;

	return score;
}

function buildTsDeclarationSpanCandidate(params: {
	target: SourceEvidenceTarget;
	contents: string;
	questionTerms: string[];
}): SourceEvidenceCandidate | undefined {
	if (!params.target.symbol) {
		return undefined;
	}

	if (!isTsJsSourcePath(params.target.sourcePath)) {
		return undefined;
	}

	const sourceFile = ts.createSourceFile(
		params.target.sourcePath,
		params.contents,
		ts.ScriptTarget.Latest,
		true,
		scriptKindForSourcePath(params.target.sourcePath)
	);
	const resolvedEntry = resolveDeclarationEntry({
		sourceFile,
		symbol: params.target.symbol,
	});

	if (!resolvedEntry) {
		return undefined;
	}

	const startPos = computeDeclarationStartPos(sourceFile, resolvedEntry.node);
	const endPos = resolvedEntry.node.getEnd();
	const startLine = sourceFile.getLineAndCharacterOfPosition(startPos).line + 1;
	const endLine = sourceFile.getLineAndCharacterOfPosition(endPos).line + 1;
	const declarationContents = params.contents.slice(startPos, endPos);
	if (!declarationContents.trim()) {
		return undefined;
	}

	const symbolTermMatches = countTokenOverlap(
		resolvedEntry.qualifiedName ?? resolvedEntry.name,
		params.questionTerms
	);
	const distinctTermMatches = countTokenOverlap(
		declarationContents,
		params.questionTerms
	);
	const totalTermMatches = countBoundedTermOccurrences(
		declarationContents,
		params.questionTerms,
		SOURCE_EVIDENCE_MAX_MATCHES_PER_TERM_PER_WINDOW
	);

	return {
		sourcePath: normalizePathForMarkdown(params.target.sourcePath),
		role: params.target.role,
		authority: params.target.authority,
		method: 'ts-declaration-span',
		startLine,
		endLine,
		symbol: resolvedEntry.qualifiedName ?? resolvedEntry.name,
		contents: declarationContents,
		score: scoreSourceEvidenceCandidate({
			role: params.target.role,
			authority: params.target.authority,
			resolution: params.target.resolution,
			method: 'ts-declaration-span',
			distinctTermMatches,
			totalTermMatches,
			symbolTermMatches,
			symbolAssociation: params.target.symbolAssociation,
			resolvedDeclarationSymbol: true,
			targetRelevanceScore: params.target.targetRelevanceScore,
		}),
		primarySourcePath: params.target.primarySourcePath,
		relationshipKind: params.target.relationshipKind,
		resolution: params.target.resolution,
	};
}

function computeDeclarationStartPos(sourceFile: ts.SourceFile, node: ts.Node): number {
	let startPos = node.getStart(sourceFile, false);
	const jsDocNodes = ts.getJSDocCommentsAndTags(node);
	for (const jsDocNode of jsDocNodes) {
		const jsDocStart = jsDocNode.getFullStart();
		if (jsDocStart >= 0) {
			startPos = Math.min(startPos, jsDocStart);
		}
	}

	return startPos;
}

function computeClippedEndLine(params: {
	contents: string;
	includedChars: number;
	startLine: number;
	endLine: number;
}): number {
	if (params.includedChars >= params.contents.length) {
		return params.endLine;
	}

	let relativeLineOffset = 0;
	for (let index = 0; index < params.includedChars; index += 1) {
		if (params.contents[index] === '\n') {
			relativeLineOffset += 1;
		}
	}

	return Math.min(
		params.endLine,
		params.startLine + relativeLineOffset
	);
}

function buildWholeFileFallbackCandidate(params: {
	target: SourceEvidenceTarget;
	contents: string;
	options: SourceEvidenceWindowOptions;
}): SourceEvidenceCandidate | undefined {
	const prefixContents = params.contents.slice(
		0,
		params.options.wholeFilePrefixMaxChars
	);

	if (!prefixContents) {
		return undefined;
	}

	const endLine =
		prefixContents.split(/\r?\n/).length;

	return {
		sourcePath: normalizePathForMarkdown(
			params.target.sourcePath
		),
		role: params.target.role,
		authority: params.target.authority,
		method: 'whole-file-prefix-fallback',
		startLine: 1,
		endLine: Math.max(1, endLine),
		symbol: params.target.symbol,
		contents: prefixContents,
		score: scoreSourceEvidenceCandidate({
			role: params.target.role,
			authority: params.target.authority,
			resolution: params.target.resolution,
			method: 'whole-file-prefix-fallback',
			distinctTermMatches: 0,
			totalTermMatches: 0,
			symbolTermMatches: countTokenOverlap(
				params.target.symbol ?? '',
				params.target.matchedTerms
			),
			symbolAssociation: params.target.symbolAssociation,
			targetRelevanceScore:
				params.target.targetRelevanceScore,
		}),
		primarySourcePath: params.target.primarySourcePath,
		relationshipKind: params.target.relationshipKind,
		resolution: params.target.resolution,
	};
}

export function buildSourceEvidenceCandidatesForFile(params: {
	target: SourceEvidenceTarget;
	contents: string;
	questionTerms: string[];
	onDeclarationResolution?: (resolved: boolean) => void;
	options?: Partial<SourceEvidenceWindowOptions>;
}): SourceEvidenceCandidate[] {
	const options: SourceEvidenceWindowOptions = {
		windowLinesBefore:
			params.options?.windowLinesBefore
			?? SOURCE_EVIDENCE_WINDOW_LINES_BEFORE,
		windowLinesAfter:
			params.options?.windowLinesAfter
			?? SOURCE_EVIDENCE_WINDOW_LINES_AFTER,
		maxMatchedWindowsPerFile:
			params.options?.maxMatchedWindowsPerFile
			?? SOURCE_EVIDENCE_MAX_MATCHED_WINDOWS_PER_FILE,
		wholeFilePrefixMaxChars:
			params.options?.wholeFilePrefixMaxChars
			?? SOURCE_EVIDENCE_WHOLE_FILE_PREFIX_MAX_CHARS,
	};

	const normalizedSourcePath = normalizePathForMarkdown(
		params.target.sourcePath
	);
	const declarationCandidate = buildTsDeclarationSpanCandidate({
		target: params.target,
		contents: params.contents,
		questionTerms: params.questionTerms,
	});

	if (params.target.symbol) {
		params.onDeclarationResolution?.(Boolean(declarationCandidate));
	}

	const lines = params.contents.split(/\r?\n/);
	const windows: WindowRange[] = [];
	const dedupedTerms = [...new Set(params.questionTerms)];

	for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
		const line = lines[lineIndex].toLowerCase();
		if (!line) {
			continue;
		}

		const matchedTerms = dedupedTerms.filter((term) =>
			line.includes(term)
		);

		if (matchedTerms.length === 0) {
			continue;
		}

		const termMatches = new Map<string, number>();
		for (const term of matchedTerms) {
			const matches = Math.min(
				SOURCE_EVIDENCE_MAX_MATCHES_PER_TERM_PER_WINDOW,
				countTermMatches(line, term)
			);
			if (matches > 0) {
				termMatches.set(term, matches);
			}
		}
		const startLine = Math.max(
			1,
			lineIndex + 1 - options.windowLinesBefore
		);
		const endLine = Math.min(
			lines.length,
			lineIndex + 1 + options.windowLinesAfter
		);

		windows.push({
			startLine,
			endLine,
			distinctTerms: new Set(matchedTerms),
			termMatches,
		});
	}

	const mergedWindows = mergeWindowRanges(windows);
	const totalMatchesForWindow = (windowRange: WindowRange): number =>
		[...windowRange.termMatches.values()].reduce(
			(sum, value) => sum + value,
			0
		);

	const selectedWindows = mergedWindows
		.sort((left, right) => {
			const distinctTermDifference =
				right.distinctTerms.size
				- left.distinctTerms.size;
			if (distinctTermDifference !== 0) {
				return distinctTermDifference;
			}

			const totalMatchDifference =
				totalMatchesForWindow(right) - totalMatchesForWindow(left);
			if (totalMatchDifference !== 0) {
				return totalMatchDifference;
			}

			if (left.startLine !== right.startLine) {
				return left.startLine - right.startLine;
			}

			return left.endLine - right.endLine;
		})
		.slice(0, options.maxMatchedWindowsPerFile)
		.sort((left, right) => left.startLine - right.startLine);

	if (selectedWindows.length === 0) {
		const fallbackCandidate =
			buildWholeFileFallbackCandidate({
				target: params.target,
				contents: params.contents,
				options,
			});
		const candidates = [
			...(declarationCandidate ? [declarationCandidate] : []),
			...(fallbackCandidate ? [fallbackCandidate] : []),
		];

		return candidates;
	}

	const windowCandidates = selectedWindows
		.map((windowRange): SourceEvidenceCandidate | null => {
			const windowContents = lines
				.slice(
					windowRange.startLine - 1,
					windowRange.endLine
				)
				.join('\n');

			if (!windowContents) {
				return null;
			}

			return {
				sourcePath: normalizedSourcePath,
				role: params.target.role,
				authority: params.target.authority,
				method: 'text-line-window' as const,
				startLine: windowRange.startLine,
				endLine: windowRange.endLine,
				symbol: params.target.symbol,
				contents: windowContents,
				score: scoreSourceEvidenceCandidate({
					role: params.target.role,
					authority: params.target.authority,
					resolution: params.target.resolution,
					method: 'text-line-window',
					distinctTermMatches: windowRange.distinctTerms.size,
					totalTermMatches: totalMatchesForWindow(windowRange),
					symbolTermMatches: countTokenOverlap(
						params.target.symbol ?? '',
						params.questionTerms
					),
					symbolAssociation: params.target.symbolAssociation,
					targetRelevanceScore:
						params.target.targetRelevanceScore,
				}),
				primarySourcePath:
					params.target.primarySourcePath,
				relationshipKind:
					params.target.relationshipKind,
				resolution: params.target.resolution,
			};
		});

	const filteredWindowCandidates = windowCandidates.filter(
		(candidate): candidate is SourceEvidenceCandidate =>
			candidate !== null
	);

	if (declarationCandidate) {
		const nonOverlappingWindows = filteredWindowCandidates.filter(
			(windowCandidate) =>
				windowCandidate.endLine < declarationCandidate.startLine
				|| windowCandidate.startLine > declarationCandidate.endLine
		);

		return [
			declarationCandidate,
			...nonOverlappingWindows,
		];
	}

	return filteredWindowCandidates;
}

function roleRank(role: SourceEvidenceRole): number {
	return role === 'primary-source' ? 0 : 1;
}

function authorityRank(authority: SourceEvidenceAuthority): number {
	if (authority === 'implementation') {
		return 0;
	}

	if (authority === 'documentation-config') {
		return 1;
	}

	if (authority === 'test') {
		return 2;
	}

	return 3;
}

function methodRank(method: SourceEvidenceMethod): number {
	if (method === 'ts-declaration-span') {
		return 0;
	}

	if (method === 'text-line-window') {
		return 1;
	}

	return 2;
}

function sortSourceEvidenceCandidates(
	left: SourceEvidenceCandidate,
	right: SourceEvidenceCandidate
): number {
	if (left.score !== right.score) {
		return right.score - left.score;
	}

	const roleDifference =
		roleRank(left.role) - roleRank(right.role);
	if (roleDifference !== 0) {
		return roleDifference;
	}

	const authorityDifference =
		authorityRank(left.authority) - authorityRank(right.authority);
	if (authorityDifference !== 0) {
		return authorityDifference;
	}

	const pathDifference =
		left.sourcePath.localeCompare(right.sourcePath);
	if (pathDifference !== 0) {
		return pathDifference;
	}

	if (left.startLine !== right.startLine) {
		return left.startLine - right.startLine;
	}

	const methodDifference =
		methodRank(left.method) - methodRank(right.method);
	if (methodDifference !== 0) {
		return methodDifference;
	}

	return left.endLine - right.endLine;
}

export function allocateSourceEvidenceChunks(params: {
	candidates: SourceEvidenceCandidate[];
	options?: Partial<AllocateSourceEvidenceOptions>;
}): {
	chunks: AllocatedSourceEvidenceChunk[];
	totalIncludedChars: number;
	chunksClippedByBudget: number;
	wholeFileFallbackChunks: number;
} {
	const options: AllocateSourceEvidenceOptions = {
		totalBudgetChars:
			params.options?.totalBudgetChars
			?? 60000,
		perFileCeilingChars:
			params.options?.perFileCeilingChars
			?? 20000,
		perChunkCapChars:
			params.options?.perChunkCapChars
			?? 12000,
	};

	const sortedCandidates = [...params.candidates]
		.sort(sortSourceEvidenceCandidates);
	const chunks: AllocatedSourceEvidenceChunk[] = [];
	const fileUsage = new Map<string, number>();
	let totalIncludedChars = 0;
	let chunksClippedByBudget = 0;
	let wholeFileFallbackChunks = 0;

	for (const candidate of sortedCandidates) {
		if (totalIncludedChars >= options.totalBudgetChars) {
			break;
		}

		const usedByFile =
			fileUsage.get(candidate.sourcePath) ?? 0;
		const remainingTotal =
			options.totalBudgetChars - totalIncludedChars;
		const remainingForFile = Math.max(
			0,
			options.perFileCeilingChars - usedByFile
		);
		const chunkContentLimit = Math.min(
			candidate.contents.length,
			options.perChunkCapChars
		);
		const includedChars = Math.min(
			chunkContentLimit,
			remainingTotal,
			remainingForFile
		);

		if (includedChars <= 0) {
			continue;
		}

		const clippedByBudget =
			includedChars < candidate.contents.length;

		if (clippedByBudget) {
			chunksClippedByBudget += 1;
		}

		if (candidate.method === 'whole-file-prefix-fallback') {
			wholeFileFallbackChunks += 1;
		}

		chunks.push({
			...candidate,
			contents: candidate.contents.slice(0, includedChars),
			includedChars,
			clippedByBudget,
			endLine: computeClippedEndLine({
				contents: candidate.contents,
				includedChars,
				startLine: candidate.startLine,
				endLine: candidate.endLine,
			}),
		});
		totalIncludedChars += includedChars;
		fileUsage.set(
			candidate.sourcePath,
			usedByFile + includedChars
		);
	}

	return {
		chunks,
		totalIncludedChars,
		chunksClippedByBudget,
		wholeFileFallbackChunks,
	};
}
