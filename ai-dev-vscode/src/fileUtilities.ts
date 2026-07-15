import * as fs from 'node:fs/promises';

export function truncateText(
	content: string,
	maxChars: number
): string {
	if (content.length <= maxChars) {
		return content;
	}

	return `${content.slice(0, maxChars)}\n...[truncated]`;
}

export async function fileExists(
	filePath: string
): Promise<boolean> {
	try {
		await fs.access(filePath);
		return true;
	} catch {
		return false;
	}
}

export async function readOptionalTextFile(
	filePath: string
): Promise<string | undefined> {
	try {
		return await fs.readFile(filePath, 'utf8');
	} catch (error) {
		if (
			(error as NodeJS.ErrnoException).code
			=== 'ENOENT'
		) {
			return undefined;
		}

		throw error;
	}
}
