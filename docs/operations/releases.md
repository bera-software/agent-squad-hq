# Releasing the hub

`.github/workflows/release.yml` runs on any `v*` tag and publishes the wheel and sdist to PyPI (trusted publishing), a signed container image to GHCR, provenance and SBOM attestations, and a GitHub release. Publishing is gated three ways.

1. **The tagged commit must be on `main`.** The `verify` job fails the run unless `git merge-base --is-ancestor <tag commit> origin/main` holds, so a tag on an unmerged branch never publishes.
2. **Only maintainers can create `v*` tags.** Add a tag ruleset (Settings → Rules → Rulesets → New tag ruleset): target `v*`, restrict creation, update and deletion, and add only the maintainers team to the bypass list. Without it, anyone with write access can trigger a release.
3. **Protected environment.** The `pypi` and `image` jobs both run in the `pypi` environment. Give it required reviewers (Settings → Environments → pypi) and limit deployment branches/tags to `v*`. The PyPI trusted publisher must name this workflow and this environment.

Build tools are installed from `requirements/release.lock` with `--require-hashes`, and the build runs with `--no-isolation`, so no unhashed build backend is fetched. To bump them, regenerate the lock:

```sh
uv pip compile --generate-hashes --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu requirements/release.in -o requirements/release.lock
```

where `requirements/release.in` lists `build==<version>` and `hatchling==<version>`.

After a release, `stub-sync` opens PRs that pin every project's stub to the new tag's commit.
