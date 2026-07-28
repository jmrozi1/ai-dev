# jenkins-controller image

This directory defines the Jenkins controller container image.

## Scope

Included:
- Base Jenkins controller image pinned to Jenkins core 2.516.3 on Java 21.
- Pinned baseline plugin manifest installed during image build via `jenkins-plugin-cli`.
- Local build script for repeatable controller image builds.
- Complete plugin lock and reproducibility verification scripts.
- Centralized controller version configuration via `versions.env`.

Excluded:
- JCasC configuration.
- Job definitions.
- Credentials management.
- Agent images or runtime wiring.
- Kubernetes manifests.
- Ansible automation.

## Base Image Pin

The Dockerfile uses the exact official Jenkins image tag:
- `jenkins/jenkins:2.516.3-jdk21`

Floating tags such as `lts` and `latest` are not used.

## Version Configuration

`versions.env` is the single controller version configuration source for reproducibility tooling.
It defines:
- `JENKINS_VERSION`
- `JAVA_VERSION`
- `JENKINS_BASE_IMAGE`
- `CONTROLLER_IMAGE`

## Plugin Manifest

`plugins.txt` is the human-maintained requested/security pin list.

Directly requested baseline plugins currently pinned in `plugins.txt`:
- `configuration-as-code:2036.v0b_c2de701dcb_`
- `job-dsl:3654.vdf58f53e2d15`
- `workflow-aggregator:608.v67378e9d3db_1`
- `git:5.10.1`
- `credentials-binding:728.v902a_273b_8947`

Selected transitive dependencies may also be explicitly pinned when required for security or compatibility.

`plugins.lock` is generated output that captures the complete resolved plugin inventory as sorted `plugin-id:version` lines.
It contains direct plugins and transitive dependencies.
`plugins.lock` is the authoritative production build input consumed by `Dockerfile`.
Lock changes require review.

Ordinary production builds install plugins from `plugins.lock`.
They do not resolve transitive versions from current update-center metadata beyond validating and downloading the exact locked versions.
Dependency resolution happens only in lock-regeneration workflow.

## Build

Run from repository root:

```bash
./image-definitions/jenkins-controller/build-controller-image.sh
```

Optional overrides:

```bash
IMAGE_NAME=my-registry/jenkins-controller IMAGE_TAG=2.516.3-jdk21 ./image-definitions/jenkins-controller/build-controller-image.sh
```

Default image naming:
- `jenkins-platform/jenkins-controller:2.516.3-jdk21`

## Local Runtime Validation

The local runtime uses Docker Compose with a Compose-managed Jenkins home named volume.
`./compose/stop-controller.sh` removes that volume, so the runtime remains disposable by default.
The runtime scripts support either Docker Compose (`docker compose`) or Podman Compose (`podman compose`).

Start controller:

```bash
./compose/start-controller.sh
```

Wait until healthcheck reports healthy:

```bash
./compose/wait-controller-healthy.sh
```

Validate runtime expectations:

```bash
./compose/validate-controller-runtime.sh
```

This validation checks:
- Jenkins HTTP readiness through the container healthcheck.
- Jenkins core version from the `X-Jenkins` HTTP response header (expected `2.516.3`).
- Installed plugin versions include every source-controlled expected version in `plugins.txt`.

Stop controller and remove runtime resources:

```bash
./compose/stop-controller.sh
```

## Version Policy

- Jenkins core and Java runtime are pinned via the Docker base image tag.
- Directly requested plugins are explicitly pinned in source control.
- Complete resolved plugin inventory is pinned in `plugins.lock`.
- Jenkins core/runtime version changes begin in `versions.env`, then require lock regeneration and full validation.
- Any version update must modify the relevant version manifests via pull request review.

## Reproducibility Workflow

Controller-local scripts:
- `extract-jenkins-core-version.sh`: prints Jenkins core version from an image.
- `extract-plugin-inventory.sh`: prints complete sorted plugin inventory from packaged `.jpi/.hpi` files in an image.
- `verify-plugin-lock.sh`: verifies Jenkins core `2.516.3` and plugin inventory against `plugins.lock`.
- `regenerate-plugin-lock.sh`: intentionally resolves `plugins.txt` and regenerates `plugins.lock`.
- `verify-rebuild-equivalence.sh`: performs two clean builds and verifies equivalent installation.

Scripts accepting an optional image reference:
- `extract-jenkins-core-version.sh`
- `extract-plugin-inventory.sh`
- `verify-plugin-lock.sh`

Scripts without image arguments:
- `regenerate-plugin-lock.sh`
- `verify-rebuild-equivalence.sh`

Default image reference:
- `jenkins-platform/jenkins-controller:2.516.3-jdk21`

### Regenerate Lock Intentionally

```bash
./image-definitions/jenkins-controller/regenerate-plugin-lock.sh
```

The script resolves `plugins.txt` in an isolated temporary resolver build, writes through a temporary file, and replaces `plugins.lock` only after successful extraction.
Verification does not rewrite the lock file.
Resolver build output remains visible; the script fails when security warning text is detected.

### Verify Image Against Lock

```bash
./image-definitions/jenkins-controller/verify-plugin-lock.sh
```

Optional image override:

```bash
./image-definitions/jenkins-controller/verify-plugin-lock.sh my-image:tag
```

On mismatch, the script exits nonzero and prints a unified diff with a regeneration command.

### Two-Build Equivalence Test

```bash
./image-definitions/jenkins-controller/verify-rebuild-equivalence.sh
```

This script:
- runs two clean image builds from current source with distinct temporary tags;
- extracts Jenkins core version and complete plugin inventory from both;
- verifies build A equals build B;
- verifies both match `plugins.lock`;
- prints diffs and exits nonzero on mismatch;
- removes temporary images on success and failure.

Equivalent installation means matching Jenkins core version and complete plugin/version inventory.
Matching image digests is not required because digest differences can come from nondeterministic image-layer metadata while the installed controller runtime content is still equivalent.

### Security Warning Visibility

`jenkins-plugin-cli` warning output remains visible during builds.
`verify-rebuild-equivalence.sh` includes a lightweight text check for `security warning` patterns in build logs and fails when detected.
This is a heuristic based on build output text, not a structured machine-readable security report.
