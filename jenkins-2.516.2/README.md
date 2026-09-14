# Jenkins 2.516.2 chart bundle

Official Jenkins chart **5.8.86**, with upstream `appVersion: 2.516.2` and
controller image `jenkins/jenkins:2.516.2-jdk21`. This is not the Bitnami chart.
The upstream archive is unmodified and has no chart dependencies.

Source: https://github.com/jenkinsci/helm-charts/releases/tag/jenkins-5.8.86
Archive: https://github.com/jenkinsci/helm-charts/releases/download/jenkins-5.8.86/jenkins-5.8.86.tgz
Checksum was checked against https://charts.jenkins.io/index.yaml.

## Inspect locally, without installing

From this directory after the repository is mirrored:

```bash
sha256sum -c SHA256SUMS
helm-stg show values ./jenkins-5.8.86.tgz
helm-stg template jenkins ./jenkins-5.8.86.tgz -n jenkins -f values.example.yaml
```

These commands require no chart repository or chart download. Rendered output
contains a generated admin Secret: inspect locally rather than posting the full
output. Rendering verifies references and templates, not registry access or startup.

The example selects `containeryard.evoforge.org/jenkins/jenkins:2.516.2-jdk21`.
With the reported RKE2 mirror/rewrite rule, that requests
`<Harbor-host>/containeryard_evo_proxy/jenkins/jenkins:2.516.2-jdk21`.
The node's actual configuration and access have not been verified here.

## Remaining offline prerequisites — before installation

This bundle supplies the chart, not an offline plugin/image archive or final
lab configuration. Do not install with only the example values.

* Translate the existing lab storage and access settings to this chart. Bitnami
  values are not interchangeable. Confirm PVC/storage class, namespace, permissions,
  ingress/service settings and any existing data before selecting deployment values.
* `plugins.txt` lists the four exact top-level plugins selected by this chart.
  Their transitive dependencies are also required. The chart normally downloads
  plugins in its init container; the stock Jenkins image does not supply this
  complete set. Provide a reachable internal plugin source or a verified preloaded
  plugin set, and only then configure offline plugin behavior. In particular, do
  not simply disable downloads: the default admin configuration relies on the
  Configuration as Code plugin loading successfully.
* The default controller pod also uses
  `docker.io/kiwigrid/k8s-sidecar:1.30.7` for configuration reload. Mirror that image
  and configure its address, or explicitly disable auto-reload if not needed.
* Default dynamic agents use
  `jenkins/inbound-agent:3327.v868139a_d00e0-7`. Supply/configure it if agents are
  enabled; it is not the controller image.
* `helm test` has its own Bats image, identified in the bundled chart's
  `helmtest.bats.image` defaults. It is not needed for ordinary controller startup.
* Configure the administrator through this chart's `controller.admin` settings
  or an existing Secret; never commit credentials to this mirrored repository.

Once a lab-specific values file covers those items, render with that file last:

```bash
helm-stg template jenkins ./jenkins-5.8.86.tgz -n jenkins -f values.example.yaml -f /path/to/lab-values.yaml
```

No installation, image push, or cluster configuration changes are performed by
this bundle. Plugin compatibility, image pulls and live startup remain unverified.
