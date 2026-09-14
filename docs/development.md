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
