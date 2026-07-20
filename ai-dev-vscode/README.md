# AI Dev VS Code

AI Dev VS Code generates, reviews, and uses compact AI navigation documentation from inside VS Code.

The extension is intentionally thin. Reusable workflow instructions, templates, profiles, conventions, and schemas live in the bundled `ai-dev-core` package.

## Features

- Launches an **AI Dev** assistant from the Activity Bar.
- Generates directory-level `summary.md` files from selected source files.
- Refreshes the architecture summary after successful grouped summarization.
- Reviews changed documentation, code, and test coverage.
- Answers project questions through generated summaries and verified source.
- Resolves evidence-backed dependencies for behavior delegated across files.
- Records included dependency context and resolution evidence in `/showreport`.
- Supports prompt-only and direct experimental workflow modes.

## Workspace configuration

AI Dev works with defaults when `.ai-dev.yaml` is absent.

A minimal explicit configuration is:

```yaml
documentation:
  docsDir: ai-docs

aiProvider:
  mode: direct-experimental
```

`aiDevCore.path` is optional for packaged installs. When it is omitted, the extension uses the bundled `vendor/ai-dev-core` directory.

## Dependency-aware summarization

Dependency-aware summarization has three separate layers:

1. **Resolver configuration** determines where deterministic dependency discovery may scan.
2. **Dependency strategy** determines which resolved relationships a matching summarization rule may follow and how much context may be included.
3. **Summarization instructions** tell the model what behavior to explain after deterministic dependency selection is complete.

Instructions do not resolve paths. Resolver logic creates evidence-backed edges first, and the strategy selects from those edges.

### 1. Resolver configuration

Jenkins resolver scope is configured in `.ai-dev.yaml`:

```yaml
dependencyResolvers:
  jenkinsConfigInclude:
    - "jobs/**/config.xml"
    - "tools/jenkins/jobs/**/config.xml"
  jenkinsConfigExclude:
    - "**/fixtures/**"
    - "**/external-context/**"
```

`jenkinsConfigInclude` limits which Jenkins `config.xml` files are scanned. When the list is empty, eligible discovered `config.xml` files are considered.

`jenkinsConfigExclude` is applied after includes and removes matching files from resolver scope.

When summarization selects a Jenkins `config.xml`, AI Dev refreshes the dependency map before generating the summary.

The current resolver records these relationship kinds:

- `jenkins-pipeline-script` — a Jenkins SCM Pipeline job to the checked-in pipeline script named by `scriptPath`.
- `shell-command-script` — a resolved Jenkins/Groovy shell command to a literal checked-in script.
- `shell-source` — a shell script to a literal file loaded through `source`, `.`, or `exec`.

Resolved delegated files are crawled recursively with deterministic safety limits. Ambiguous and unresolved references are recorded and surfaced as warnings rather than guessed.

The generated dependency map is stored under the configured documentation directory as:

```text
ai-docs/dependency-map.json
```

### 2. Dependency strategy

Summarization rules are stored in `.ai-dev-summarization.json`. They can be edited through `/settings`.

Example:

```json
{
  "version": 1,
  "generalInstructions": "Summarize behavior and purpose rather than merely describing syntax.",
  "rules": [
    {
      "id": "jenkins-job",
      "name": "Jenkins job configuration",
      "glob": "**/jobs/**/config.xml",
      "priority": 100,
      "enabled": true,
      "instructions": "Explain the effective pipeline behavior, outputs, side effects, and responsibility boundaries.",
      "dependencyStrategy": {
        "follow": [
          "jenkins-pipeline-script"
        ],
        "maxDepth": 5,
        "maxFiles": 20,
        "maxChars": 60000,
        "includeInferred": true
      }
    }
  ]
}
```

Dependency strategy fields:

- `follow` — relationship kinds allowed from the primary source. Downstream traversal may continue through resolved dependency relationships.
- `maxDepth` — maximum traversal depth, from 1 through 10. Depth 1 includes only direct dependencies.
- `maxFiles` — maximum dependency files included for one primary source.
- `maxChars` — maximum combined dependency-context characters for one primary source.
- `includeInferred` — when `false`, only exact edges are eligible; when `true`, inferred edges may also be used.

When multiple enabled rules match, their instructions are combined in priority order. The last matching rule that defines a dependency strategy supplies the active strategy.

Rules without a dependency strategy preserve ordinary summarization behavior.

### 3. Summarization instructions

Instructions describe the desired summary outcome. For example:

```text
Explain the effective pipeline behavior, generated artifacts, failure boundaries, and what the job deliberately does not deploy or publish.
```

Instructions may tell the model what to emphasize, but they must not ask it to guess file relationships. Missing or ambiguous dependencies remain warnings.

## Dependency reporting

After `/summarize`, `/showreport` displays deterministic information about dependency context selected for that operation:

- primary source path;
- included dependency path;
- relationship kind;
- exact or inferred resolution;
- evidence explaining why the dependency was selected.

Dependency file contents are not duplicated into the report.

## Commands

Common terminal commands include:

```text
/ask <question>
/summarize <file-or-glob>
/review
/settings
/showreport
/help
```

Use `/summarize --smoketest <glob>` to preview scope without executing model calls.

## Build

From the combined repository root:

```bash
./scripts/build-vsix.sh
```

The packaged extension is written to `artifacts/`.

## Install

Install the current packaged release:

```bash
code --install-extension artifacts/ai-dev-vscode-0.1.0.vsix --force
```

## Design boundaries

- Source and executable configuration remain authoritative.
- Dependency relationships must be evidence-backed.
- Ambiguous references are not silently guessed.
- Traversal is bounded by depth, file-count, and character budgets.
- Summaries explain behavior without becoming exhaustive implementation manuals.
- Summary answers may be verified against source rather than treating generated documentation as final authority.
