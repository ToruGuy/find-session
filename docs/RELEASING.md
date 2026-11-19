# Releasing

## Every release

1. On an up-to-date `main` with green CI: `python3 scripts/release.py` proposes the next version from `## [Unreleased]`.
2. `python3 scripts/release.py --apply X.Y.Z` writes it to `VERSION`, `skills/find-session/VERSION`, `.claude-plugin/plugin.json` and `package.json`, and moves the entries under `## [X.Y.Z] - <today>`.
3. Review the diff, then `git commit -am "release: X.Y.Z"` and `git push origin main`.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. The `release` workflow checks the tag against the files and the changelog, runs the tests, builds the npm tarball and the Python sdist and wheel, and creates the GitHub Release with that changelog section as notes and the files attached. Then it publishes to npm and PyPI, each only when enabled below. Actions → release → Run workflow is a dry run: it checks and builds but creates nothing.

Before the first real tag, do the dry run once from the Actions tab: it exercises everything except the release and the publish jobs. `release.py` never commits, tags or pushes. A failed registry job can be re-run from the Actions page. Re-running the release job refreshes the files of an existing release.

## One-time setup

All of this is done in the GitHub and registry web interfaces. Until it is done, releases still work and the registry jobs are skipped with a notice.

### GitHub

- **Private vulnerability reporting**, referenced by `SECURITY.md`: Settings → Advanced Security (called "Code security" on some accounts) → Private vulnerability reporting → Enable. It is available once the repository is public.
- The workflow asks for the permissions it needs per job: `contents: write` only for creating the release, `id-token: write` only for publishing. If an organisation policy caps workflow permissions at read-only, allow write for this repository (Settings → Actions → General → Workflow permissions).
- The jobs use the environments `npm` and `pypi`. GitHub creates them on first use. Add a required reviewer to one of them (Settings → Environments) if a publish should wait for a click.

### npm: package `find-session`

The repository must be public before any publish from CI, because `--provenance` fails for a private repository. As of this writing, npm attaches a trusted publisher only to a package that already exists, so the first version goes up by a different route. If npmjs.com lets you add the publisher before the first publish, skip step 1.

1. Publish the first version by one of these two routes:
   - **By hand:** `npm login`, then `npm publish find-session-X.Y.Z.tgz --access public`, using the tarball attached to the GitHub Release (npm asks for 2FA).
   - **From CI:** create a granular access token with publish rights and a short expiry, store it as the repository secret `NPM_TOKEN`, and set the variable from step 3 before tagging.
2. On npmjs.com, open the package → Settings → Trusted publishing → GitHub Actions. Enter organisation `ToruGuy`, repository `find-session`, workflow `release.yml` and environment `npm`. A new trusted publisher expires unless it publishes within 2 days, so set it up shortly before a release.
3. Set the repository variable `NPM_PUBLISH` to `true` (Settings → Secrets and variables → Actions → Variables).
4. Delete the `NPM_TOKEN` secret if you created one. In the package settings choose "Require two-factor authentication and disallow tokens"; trusted publishing keeps working.

Trusted publishing needs npm 11.5.1 or newer; the workflow upgrades npm when the runner's is older. Provenance is generated automatically when the repository and the package are public. It also requires `repository.url` in `package.json` to match the GitHub repository, which it does.

### PyPI: distribution `find-session-cli`

The command stays `find-session` and the import package `find_session`; only the distribution name differs, because `find-session` is taken on PyPI by another project.

1. On pypi.org, go to Account → Publishing → Add a new pending publisher. Enter project `find-session-cli`, owner `ToruGuy`, repository `find-session`, workflow `release.yml` and environment `pypi`.
2. Set the repository variable `PYPI_PROJECT` to `find-session-cli`. The workflow stops before releasing anything if this variable and `name` in `pyproject.toml` disagree.
3. After the first successful upload, change the "command on your PATH" row in `README.md` to `uv tool install find-session-cli` / `pipx install find-session-cli`.

**Why not `find-session`:** that name belongs to another project on PyPI, and PyPI treats names that differ only in `.`, `_` or `-` as the same (`ultranormalize_name` in pypi/warehouse), so a spelling without the hyphen would be refused too. `find-session-cli` differs in letters. To use another name, change `name` in `pyproject.toml` and the `PYPI_PROJECT` variable; the command stays `find-session`.
