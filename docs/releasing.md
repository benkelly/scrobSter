# Releasing

The version lives in one place, `__version__` in `scrobster/__init__.py`, and
`GET /api/status` reports it.

To cut a release, bump that number in the pull request. When it merges, CI
publishes the image as `ghcr.io/benkelly/scrobster:<version>`, moves the
matching `x.y` tag, pushes the git tag `v<version>` and opens a GitHub release
with generated notes. A merge that does not change the number publishes only
`latest` and the commit tag, so ordinary work needs no bump. The Helm chart is
published as `oci://ghcr.io/benkelly/charts/scrobster:<version>` whenever that
version is not there yet, with the same number for the chart and the app.

## Verifying

CI signs the image and the chart by digest with keyless
[cosign](https://github.com/sigstore/cosign), so a signature proves the artifact
was pushed by this repository's workflow on `main`. The identity keeps the
repository's own case, `benkelly/scrobSter`:

```sh
cosign verify ghcr.io/benkelly/scrobster:<version> \
  --certificate-identity-regexp '^https://github\.com/benkelly/scrobSter/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify ghcr.io/benkelly/charts/scrobster:<version> \
  --certificate-identity-regexp '^https://github\.com/benkelly/scrobSter/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The image also carries an SPDX SBOM and SLSA provenance from BuildKit:

```sh
docker buildx imagetools inspect ghcr.io/benkelly/scrobster:<version> \
  --format '{{ json .SBOM }}'
docker buildx imagetools inspect ghcr.io/benkelly/scrobster:<version> \
  --format '{{ json .Provenance }}'
```

The Home Assistant add-on pins the exact version, so bump
[ha-addons](https://github.com/benkelly/ha-addons) after the release, never
before: the add-on cannot pull a tag that has not been built.
