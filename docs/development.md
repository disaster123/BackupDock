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
3. Merge the release commit to `main` and wait for the test workflow to pass.
4. Create and push an annotated matching tag, for example:

   ```bash
   git tag -a v0.3.20 -m "BackupDock 0.3.20"
   git push origin v0.3.20
   ```

The release workflow verifies the tag format, both package version declarations and the changelog. It reruns the complete test suite on Python 3.11, 3.12 and 3.13, then creates the matching GitHub Release from the changelog section. A failed validation or test prevents publication.
