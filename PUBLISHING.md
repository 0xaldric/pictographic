# Publishing `centerline-svg` to (Test)PyPI

Publish to **TestPyPI** first (a throwaway sandbox), verify the install, then push the exact
same artifacts to **real PyPI**. Uses the modern `pyproject.toml` + `build` + `twine` flow.

## 0. One-time setup

```bash
python -m pip install --upgrade build twine
```

- `build` — builds the wheel + sdist from `pyproject.toml`.
- `twine` — uploads the built artifacts and checks their metadata.

Create two accounts (separate systems, separate passwords):
- TestPyPI: https://test.pypi.org/account/register/
- PyPI:     https://pypi.org/account/register/

Then create an **API token** on each (Account settings → API tokens → *Add API token*,
scope "Entire account" for the first upload). A token looks like `pypi-AgEIcHl...`.
Treat it like a password.

## 1. Pick a unique name & set the version

- **Name** (`[project].name` in `pyproject.toml`) must be **globally unique** on the index.
  `centerline-svg` may be taken on TestPyPI (anyone can grab names there). If `twine` reports
  `403 … name already in use`, change `name = "..."` to something unique
  (e.g. `centerline-svg-anhnd`) and rebuild.
- **Version** (`[project].version`) can **never be reused or overwritten** on (Test)PyPI. Every
  upload needs a new version. Bump it (`0.1.0` → `0.1.1`) for each release. During testing you
  will burn version numbers on TestPyPI — that's fine.

## 2. Build the distribution

```bash
rm -rf dist/
python -m build          # writes dist/centerline_svg-<ver>.tar.gz (sdist) + ...-py3-none-any.whl (wheel)
twine check dist/*       # validates the README renders + metadata is well-formed
```

## 3. Upload to TestPyPI

```bash
twine upload --repository testpypi dist/*
```

`twine` will prompt for a username and password. For token auth use:
- username: `__token__` (literally)
- password: the TestPyPI token (`pypi-...`)

To avoid the prompt, either set env vars for the command:

```bash
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-<your-testpypi-token> \
  twine upload --repository testpypi dist/*
```

…or store credentials once in `~/.pypirc`:

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
  username = __token__
  password = pypi-<your-PyPI-token>

[testpypi]
  repository = https://test.pypi.org/legacy/
  username = __token__
  password = pypi-<your-TestPyPI-token>
```

(`chmod 600 ~/.pypirc`.) After a successful upload the page is:
`https://test.pypi.org/project/centerline-svg/`.

## 4. Verify the install from TestPyPI

Install into a **fresh virtualenv** so you're testing the published artifact, not your source:

```bash
python -m venv /tmp/verify && source /tmp/verify/bin/activate
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            centerline-svg
python -c "import centerline_svg; print(centerline_svg.png_to_svg('some.png')[:60])"
centerline-svg some.png /tmp/out.svg
deactivate
```

**Why `--extra-index-url`:** TestPyPI does not mirror real PyPI, so any *dependencies* your
package needs must be fetched from real PyPI. `centerline-svg` itself has **zero** runtime
dependencies, so this is not strictly required here — but it's the safe habit, and you need it
the moment you add a dependency.

## 5. Publish to real PyPI

Once the TestPyPI install works, upload the **same `dist/`** to PyPI — one flag different:

```bash
twine upload dist/*          # no --repository → defaults to real PyPI
```

Then `pip install centerline-svg` works for everyone. Project page:
`https://pypi.org/project/centerline-svg/`.

## Gotchas checklist

- [ ] Version bumped (can't overwrite an existing one on the index).
- [ ] Name is unique on the target index (change it if `twine` 403s).
- [ ] `readme = "README.md"` in `pyproject.toml` and `twine check dist/*` passes (this is the
      long-description shown on the project page; a broken README fails the upload).
- [ ] Tested in a **clean venv**, not your dev environment.
- [ ] TestPyPI and PyPI tokens are **different** (different sites, different scopes).

## Alternative: Trusted Publishing (OIDC, no long-lived tokens)

For CI (e.g. GitHub Actions), the modern recommendation is **Trusted Publishing**: register the
repo as a "trusted publisher" for the project on PyPI/TestPyPI (Project → Publishing), then in a
GitHub Actions workflow use `pypa/gh-action-pypi-publish` — it mints a short-lived OIDC token, so
no API token is stored as a secret. See
https://docs.pypi.org/trusted-publishers/ . For a first manual release, the token flow above is
simplest.
