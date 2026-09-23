# Releasing

How to cut a release for django-boundary.

## TL;DR

```
branch  ->  PR  ->  review  ->  merge to main  ->  tag the merged commit  ->  CI publishes
```

The **tag is the trigger**. Pushing a tag of the form `v<semver>` runs the
publish workflow, which tests, builds, uploads to PyPI (OIDC trusted
publishing, no token), and creates a GitHub release. **Tagging is publishing.
There is no separate "publish" step and no undo.**

## Canonical flow

1. **Branch** off `main`:
   `release/<version>` (e.g. `release/0.5.0`).
2. **Bump** on the branch:
   - `pyproject.toml` - update `version`
   - `src/boundary/__init__.py` - update `__version__` (keep in sync)
   - `CHANGELOG.md` - rename `[Unreleased]` to `[<version>] - <YYYY-MM-DD>`
3. **Open a PR** to `main`. CI runs lint and tests. Get it reviewed. This is
   the gate, do not skip it.
4. **Merge to `main`** (squash or merge, per repo norm).
5. **Tag the merged commit on `main`** and push the tag:
   ```bash
   git checkout main && git pull
   git tag v<version>
   git push origin v<version>
   ```
6. **Watch the publish run** and confirm PyPI:
   ```bash
   gh run watch <run-id> --exit-status
   curl -s https://pypi.org/pypi/django-boundary/json | python -c \
     "import sys,json;print(json.load(sys.stdin)['info']['version'])"
   ```

> **Tag the commit that is on `main`, not a feature branch.** Tags point at
> commits, not branches, so tagging a feature branch *will* publish, but it
> publishes code that may never have been merged. Always tag after the merge so
> what is on PyPI is exactly what is on `main`.

## Tag format (strict)

`v<semver>`: the letter `v` followed by the version number. The publish
workflow matches `v*` and parses the version from after `v`.

| Version | Tag example |
| --- | --- |
| 0.5.0 | `v0.5.0` |
| 1.0.0 | `v1.0.0` |
| 1.2.3 | `v1.2.3` |

## Versioning (SemVer)

[Semantic Versioning](https://semver.org/). Pre-1.0, the rules still apply
with the usual pre-1.0 caveat that minor bumps may carry breaking changes:

- **Patch** (`0.4.0 -> 0.4.1`): bug fixes, doc-only changes, no API or
  behaviour change.
- **Minor** (`0.4.1 -> 0.5.0`): new public API, **any behaviour change** (even
  a safer one), or a raised minimum dependency floor (e.g. Django).
- **Major** (`0.x -> 1.0`): the stability commitment; breaking changes after
  1.0.

If in doubt between patch and minor, choose minor. Burning a version number is
free; shipping a behaviour change as a patch surprises consumers.

## CHANGELOG (required, every release)

**A release MUST include a CHANGELOG entry for its version. No entry, no tag.**
Every published version needs a dated section in `CHANGELOG.md`. CI enforces
this: the publish workflow will fail if no matching `## [<version>]` heading
exists.

[Keep a Changelog](https://keepachangelog.com/) format. Accumulate entries
under `## [Unreleased]` as you work; at release time, rename that heading to
`## [<version>] - <YYYY-MM-DD>`. Subsections: Added / Changed / Fixed /
Removed. Call out behaviour changes explicitly, including ones that are
"safer", because a consumer relying on the old behaviour still needs to know.

The GitHub release body is auto-generated from the tag, but that is **not** a
substitute for the curated CHANGELOG entry. Write the CHANGELOG by hand so
consumers reading the package on PyPI or GitHub get a human-authored summary,
not just a commit list.

## Version locations

The version lives in exactly two places. Both must match at the time of
tagging:

- `pyproject.toml` under `[project]` -> `version`
- `src/boundary/__init__.py` -> `__version__`

## Keep the CI Django matrix in step with the floor

The publish workflow's test job runs a Django matrix (BR-ENV-006), one leg per
supported version, each installing `Django~=<version>.0` against the built
wheel; `ci.yml`'s test job runs the same axis. When you raise the minimum
Django in `pyproject.toml`, or add or drop a supported version, **update the
`django-version` matrix in both workflows in the same PR**, and the
`Framework :: Django ::` classifiers with them. A matrix leg below the new
floor fails to resolve dependencies and blocks the publish; a supported
version missing from the matrix ships untested.

## Pre-tag checklist

Before pushing the tag (the irreversible step):

- [ ] **CHANGELOG has a `[<version>] - <date>` entry** (renamed from
      `[Unreleased]`). This is mandatory.
- [ ] Behaviour changes and breaking changes called out in that CHANGELOG entry.
- [ ] Version bumped in `pyproject.toml` **and** `src/boundary/__init__.py`,
      and they match.
- [ ] CI Django matrix matches the supported versions, if the floor or the
      supported set changed.
- [ ] Tests pass locally and the package builds (`python -m build`).
- [ ] The PR is **merged to `main`** and you are tagging that commit.
- [ ] Tag format is `v<version>`.
- [ ] This exact version has never been published (PyPI rejects re-uploads).

## If something goes wrong

- **PyPI rejects the upload (version exists).** That version is permanently
  taken. You cannot re-upload, even after deleting. Bump to the next patch
  and re-tag.
- **The test or build job fails after tagging.** Nothing was published (publish
  is the last job and depends on test and build). Fix on a new PR, merge,
  delete the bad tag (`git push --delete origin v<version>`), and re-tag the
  new commit with the **same** version (since nothing reached PyPI).
- **Published, but the code is not on `main`.** Open a PR from the release
  branch to `main` immediately and merge, so `main` reflects what is on PyPI.
  Avoid this by always tagging after the merge.

## Optional hardening

Consider adding a **manual approval gate** to the `publish` job via a
protected GitHub Environment (`pypi`), so "push tag" and "irreversibly upload
to PyPI" are decoupled. A human approves the upload after seeing test and
build go green. The workflow already declares `environment: pypi`; add a
required-reviewer protection rule to that environment to enable the gate.
