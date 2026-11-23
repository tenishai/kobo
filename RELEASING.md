# Releasing

A release is a tag. Pushing `vX.Y.Z` runs `.github/workflows/release.yml`, which tests the
tagged commit, builds and checks the image, publishes it, and writes the GitHub release.
Nothing else builds a release image, and nobody uploads anything by hand.

## Cut a release

1. Open a pull request titled `release: kobo X.Y.Z` that
   - sets `version` in `api/pyproject.toml` (and runs `uv lock` in `api/` so the lock agrees),
   - renames `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD` and adds a new
     empty `## [Unreleased]` above it.
2. Merge it once `ci` is green.
3. Tag the merge commit and push the tag (maintainers only):

   ```
   git checkout main && git pull
   git tag -a vX.Y.Z -m "kobo X.Y.Z"
   git push origin vX.Y.Z
   ```

4. Watch the `release` workflow. When it is green the release exists.

Versions follow [Semantic Versioning](https://semver.org/). A tag is never moved or deleted;
a bad release is followed by a new one.

## What a release contains

| Thing | Where |
| --- | --- |
| Image, linux/amd64 and linux/arm64 | `ghcr.io/tenishai/kobo:vX.Y.Z` and `:<short sha>`; the digest is in the release text |
| SBOM and build provenance | attached to the image, and the SBOM as `kobo-X.Y.Z.spdx.json` on the release |
| Wheel and sdist | files on the release |
| `SHA256SUMS` | checksums of every file on the release |
| Notes | this version's section of `CHANGELOG.md` |

Pull by digest when you deploy: a tag names a release, a digest names the bytes.

## What stops a release

Each of these fails the workflow before anything is published:

- any job in `ci.yml` on the tagged commit;
- the tag and `api/pyproject.toml` disagree on the version;
- the image does not start;
- a Python package in the image has a licence we cannot ship, or none in its metadata
  (`scripts/check_licences.py`);
- the image has a high or critical vulnerability **that has a fix**. Findings without a fix
  do not block. `.grype.yaml` lists what the scan leaves out, with the reason.

When the scan stops a release, fix it in a pull request (bump the package in `api/uv.lock`, or
the tool version in `docker/base/Dockerfile` and the base digest in the `Dockerfile`), release that as the next patch
version, and leave the failed tag as it is.

## Build a tag again

```
gh workflow run release.yml --ref vX.Y.Z
```

This rebuilds and republishes that tag's image and files. Use it when a run failed for a
reason outside the code (a registry outage), not to change what a version contains.
