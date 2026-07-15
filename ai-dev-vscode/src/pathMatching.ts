import {
	normalizePathForMarkdown,
} from './workspace';

export const NON_SOURCE_ARTIFACT_EXCLUDE_GLOBS = [
	'*.vsix',
	'**/*.vsix',
	'*.zip',
	'**/*.zip',
	'*.7z',
	'**/*.7z',
	'*.rar',
	'**/*.rar',
	'*.tar',
	'**/*.tar',
	'*.tgz',
	'**/*.tgz',
	'*.gz',
	'**/*.gz',
	'*.exe',
	'**/*.exe',
	'*.dll',
	'**/*.dll',
	'*.so',
	'**/*.so',
	'*.dylib',
	'**/*.dylib',
	'*.bin',
	'**/*.bin',
	'*.class',
	'**/*.class',
	'*.jar',
	'**/*.jar',
	'*.war',
	'**/*.war',
	'*.pdb',
	'**/*.pdb',
];

export function globToRegExp(globPattern: string): RegExp {
	const normalizedGlob = normalizePathForMarkdown(globPattern.trim());
	let regexSource = '';

	for (let index = 0; index < normalizedGlob.length; index += 1) {
		const char = normalizedGlob[index];
		if (char === '*') {
			const isDoubleStar = normalizedGlob[index + 1] === '*';
			if (isDoubleStar) {
				const hasFollowingSlash = normalizedGlob[index + 2] === '/';
				if (hasFollowingSlash) {
					regexSource += '(?:.*/)?';
					index += 2;
				} else {
					regexSource += '.*';
					index += 1;
				}
				continue;
			}

			regexSource += '[^/]*';
			continue;
		}

		if (char === '?') {
			regexSource += '[^/]';
			continue;
		}

		if (/[-/\\^$+?.()|[\]{}]/.test(char)) {
			regexSource += `\\${char}`;
			continue;
		}

		regexSource += char;
	}

	return new RegExp(`^${regexSource}$`);
}

export function matchesAnyGlob(relativePath: string, globs: string[]): boolean {
	const normalizedRelativePath = normalizePathForMarkdown(relativePath);
	return globs.some((globPattern) => globToRegExp(globPattern).test(normalizedRelativePath));
}
