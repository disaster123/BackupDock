# BackupDock Development

[Documentation index](README.md) · [Main README](../README.md)


## Development environment

For development, a local virtual environment can still be used:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Run the checks

```bash
bash -n install.sh uninstall.sh
python -c 'from pathlib import Path; from backupdock.config import load_config; load_config(Path("config.yaml.example"))'
python -m unittest discover -s tests -v
```

The GitHub Actions test matrix runs the same checks on Python 3.11, 3.12, and 3.13.

Changes should preserve the safety rule that discovery and validation finish before any container is stopped.

## Publishing a Release

Release creation is automated. Before publishing:

1. Update the version in `pyproject.toml` and `src/backupdock/__init__.py`.
2. Add the matching `CHANGELOG.md` section.
3. Merge the release commit to `main`.

When `main` contains a package version without a matching GitHub Release, the release workflow verifies both package version declarations and the changelog, reruns the complete test suite on Python 3.11, 3.12 and 3.13, creates a missing annotated `vX.Y.Z` tag and publishes the GitHub Release from the changelog section. If the tag already exists, the workflow verifies the remote annotated tag and tests that exact commit before publishing the missing release. A failed validation or test prevents publication.

Manually pushed annotated version tags remain supported and pass through the same validation and tests. Normal commits for an already tagged package version do not republish the release.
