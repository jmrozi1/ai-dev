export type ArchitectureSummaryStatus =
	| 'missing'
	| 'empty'
	| 'exists';

export interface ArchitectureSummaryPreviewItem {
	apply: boolean;
	sourceDirectory: string;
	summaryPath: string;
	status: ArchitectureSummaryStatus;
	notes: string;
}

export interface ArchitectureSummaryPreviewCounts {
	totalDirectories: number;
	existingSummaries: number;
	missingSummaries: number;
	emptySummaries: number;
}

export interface ArchitectureSummaryPreviewResult {
	counts: ArchitectureSummaryPreviewCounts;
	items: ArchitectureSummaryPreviewItem[];
}
