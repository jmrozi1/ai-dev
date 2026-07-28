# Jenkins Platform Repository

This repository contains the foundational structure for managing a Jenkins platform as code.
It is organized so image build inputs, local development runtime, platform configuration, pipeline code, tests, and documentation can evolve independently.

## Directory Overview

- `image-definitions/`: Source files used to build Jenkins and related container images.
- `compose/`: Local Docker Compose environment for development and platform bring-up.
- `jcasc/`: Reusable Jenkins Configuration as Code definitions.
- `job-dsl/`: Jenkins Job DSL definitions for seed and managed jobs.
- `shared-library/`: Jenkins Shared Library source used by pipelines.
- `environments/dev/`: Development environment-specific configuration and overrides.
- `tests/`: Repository and platform validation assets.
- `docs/`: Platform documentation and operational guides.
- `scripts/`: Utility scripts for repository workflows and developer tasks.

## Status

Initial scaffold with repository validation automation. Functional Jenkins platform configuration, image definitions, jobs, and runtime verification tests are not included yet.

## Repository Validation

Run the lightweight repository structure validation script:

```bash
./scripts/validate-repo.sh
```

The script checks expected scaffold directories and placeholder README files, and confirms common secret/runtime/generated files are not tracked by git.
