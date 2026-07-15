export type BatchUnitDocStatus = 'missing' | 'empty' | 'exists';
export type BatchUnitDocActionType = 'generate-doc' | 'move-doc' | 'delete-doc';

export interface BatchUnitDocPreviewItem {
	apply: boolean;
	sourcePath?: string;
	docPath: string;
	actionType: BatchUnitDocActionType;
	actionLabel: 'Update summary' | 'Move orphan doc' | 'Delete orphan doc';
	notes: string;
	targetDocPath?: string;
}

export interface BatchUnitDocPreviewCounts {
	totalConfiguredSourceCandidates: number;
	afterGlobFilter: number;
	afterMissingDocFilter: number;
	previewCount: number;
}

export interface BatchUnitDocPatternWarning {
	message: string;
	configuredPattern: string;
	recommendedPattern: string;
}

export interface BatchUnitDocPreviewResult {
	counts: BatchUnitDocPreviewCounts;
	items: BatchUnitDocPreviewItem[];
	flatteningPatternWarning?: BatchUnitDocPatternWarning;
}

export interface BatchUnitDocsFormState {
	sourceGlob: string;
	missingDocsOnly: boolean;
	resolveOrphanedDocs: boolean;
	maxFiles: number;
	selectionMode: 'workspace' | 'folder';
	selectedSourceDirectory?: string;
	selectedSummaryFile?: string;
}

export const DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS = 25;
export const MAX_BATCH_UNIT_DOC_FILES_THIS_PASS = 10000;
export const LARGE_BATCH_UNIT_DOC_RUN_WARNING_THRESHOLD = 100;
export const BATCH_UNIT_DOC_FILES_THIS_PASS_HELP_TEXT =
	'Limits how many matching source files are included in this generation pass (1-10000). Larger runs can take a while and may hit provider throttling. Generation is sequential and can be cancelled.';

export function normalizeBatchUnitDocFilesThisPass(
	value: unknown
): number {
	const numericValue =
		typeof value === 'number'
			? value
			: Number.parseInt(
				String(value ?? '').trim(),
				10
			);

	if (!Number.isFinite(numericValue)) {
		return DEFAULT_BATCH_UNIT_DOC_FILES_THIS_PASS;
	}

	return Math.min(
		MAX_BATCH_UNIT_DOC_FILES_THIS_PASS,
		Math.max(1, Math.floor(numericValue))
	);
}

export function shouldShowLargeBatchUnitDocRunWarning(
	filesThisPass: number
): boolean {
	return filesThisPass >
		LARGE_BATCH_UNIT_DOC_RUN_WARNING_THRESHOLD;
}

export function getLargeBatchUnitDocRunWarningMessage(
	filesThisPass: number
): string {
	return `Large run advisory: ${filesThisPass} files in this pass can take a while and may encounter provider throttling. Processing is sequential and can be cancelled.`;
}
