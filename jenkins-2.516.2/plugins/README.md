# Configuration as Code plugin for staging Jenkins

Plugin: configuration-as-code
Version: 1985.vdda_32d0c4ea_b_
Minimum Jenkins core: 2.504.1
Target Jenkins core: 2.516.2
Pinned by Jenkins Helm chart 5.8.86.

The HPI is the original binary archive from:
https://updates.jenkins.io/download/plugins/configuration-as-code/1985.vdda_32d0c4ea_b_/configuration-as-code.hpi

Verify the transferred file from this directory:
```sh
sha256sum -c SHA256SUMS
```

## Required dependencies

These are the minimum direct dependency versions from the plugin manifest.
Dependency archives are not included. Check installed versions and their own
dependencies before restarting Jenkins; keep newer compatible versions.

- caffeine-api:3.2.0-166.v72a_6d74b_870f
- commons-lang3-api:3.18.0-98.v3a_674c06072d
- commons-text-api:1.13.1-176.v74d88f22034b_
- json-api:20250517-153.vc8a_a_d87c0ce3
- prism-api:1.30.0-1
- snakeyaml-api:2.3-125.v4d77857a_b_402

## Installation handoff

Use staging wrappers only. The active Jenkins release is in namespace default.
Install using Jenkins Advanced plugin upload, or place the archive in the verified
persistent /var/jenkins_home/plugins directory as configuration-as-code.jpi,
with ownership matching existing plugins. Avoid duplicate .hpi/.jpi copies of
the same plugin. Restart staging Jenkins after dependencies are satisfied.

Verify that Configuration as Code loads successfully and reads the intended
OIDC YAML. Then test that login redirects to Keycloak and that the expected
permissions are granted. Installing this plugin alone does not prove OIDC works.
Keep online plugin downloads disabled for this air-gapped environment.

Validation performed: ZIP integrity and manifest inspected; Jenkins core minimum
checked. Runtime loading and installed dependency versions require staging checks.
