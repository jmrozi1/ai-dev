import * as fs from 'node:fs/promises';
import * as path from 'node:path';

export async function readGenerateUnitDocWorkflow(aiDevCoreRoot: string): Promise<string> {
	const workflowPath = path.join(aiDevCoreRoot, 'workflows', 'generate-docs', 'generate-unit-doc.md');
	return fs.readFile(workflowPath, 'utf8');
}

export async function readUnitDocTemplate(aiDevCoreRoot: string): Promise<string> {
	const templatePath = path.join(aiDevCoreRoot, 'workflows', 'generate-docs', 'templates', 'unit-doc.md');
	return fs.readFile(templatePath, 'utf8');
}

export async function readReviewDocumentationWorkflow(aiDevCoreRoot: string): Promise<string> {
	const workflowPath = path.join(aiDevCoreRoot, 'workflows', 'review', 'review-documentation.md');
	return fs.readFile(workflowPath, 'utf8');
}

export async function readReviewFindingTemplate(aiDevCoreRoot: string): Promise<string> {
	const templatePath = path.join(aiDevCoreRoot, 'workflows', 'review', 'finding-template.md');
	return fs.readFile(templatePath, 'utf8');
}