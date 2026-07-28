# compose

This directory will contain Docker Compose files and related assets for local Jenkins platform development.
Runtime data should stay out of version control.

## Jenkins Controller Runtime

- Compose definition: `compose/jenkins-controller.compose.yaml`
- Service name: `controller`
- Image: `jenkins-platform/jenkins-controller:2.516.3-jdk21`
- Jenkins HTTP endpoint: `http://127.0.0.1:${JENKINS_HTTP_PORT:-8080}`
- Jenkins home storage: Compose-managed named volume at `/var/jenkins_home`
- Disposal behavior: `compose/stop-controller.sh` removes the named volume
- Compose provider: Docker Compose (`docker compose`) or Podman Compose (`podman compose`)
- Shared command detection helper: `compose/compose-command.sh`

Helper scripts:
- `compose/start-controller.sh`
- `compose/wait-controller-healthy.sh`
- `compose/validate-controller-runtime.sh`
- `compose/stop-controller.sh`
