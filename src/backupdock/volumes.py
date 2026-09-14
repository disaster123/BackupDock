from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from backupdock.models import MountInfo


def resolve_volume_mount(mount: MountInfo, attrs: dict) -> MountInfo:
    """Resolve data that remains accessible after Docker unmounts a volume."""
    if not isinstance(attrs, dict) or attrs.get("Name") != mount.volume_name:
        return replace(mount, backup_error="Volume inspection returned invalid identity")
    driver = attrs.get("Driver")
    mount = replace(mount, volume_driver=driver if isinstance(driver, str) else None)
    if driver != "local":
        return replace(mount, backup_error="Unsupported volume driver; its mountpoint may disappear after container stop")
    if not Path(mount.source).is_absolute():
        return replace(mount, backup_error="Docker volume has no absolute source path")
    options = attrs.get("Options")
    if options is None:
        options = {}
    if not isinstance(options, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in options.items()):
        return replace(mount, backup_error="Volume inspection returned invalid driver options")
    if set(options) - {"type", "device", "o", "size"}:
        return replace(mount, backup_error="Unsupported local volume driver options")
    if not any(options.get(key) for key in ("type", "device", "o")):
        return replace(mount, backup_source=str(Path(mount.source).resolve()))

    flags = {flag.strip() for flag in options.get("o", "").split(",")}
    device = options.get("device", "")
    if not flags.intersection({"bind", "rbind"}) or options.get("type", "") not in {"", "none"} or not Path(device).is_absolute():
        return replace(mount, backup_error="Unsupported mounted local volume (network/block-device or unresolved bind source)")
    root_text = attrs.get("Mountpoint")
    if not isinstance(root_text, str) or not Path(root_text).is_absolute() or not Path(mount.source).is_absolute():
        return replace(mount, backup_error="Volume inspection returned an invalid mountpoint")
    root = Path(os.path.abspath(root_text))
    source = Path(os.path.abspath(mount.source))
    if not source.is_relative_to(root):
        return replace(mount, backup_error="Container volume source is outside its inspected mountpoint")
    # Preserve a mounted subdirectory while using the stable device path.
    stable_root = Path(device).resolve()
    stable_source = (stable_root / source.relative_to(root)).resolve()
    if not stable_source.is_relative_to(stable_root):
        return replace(mount, backup_error="Volume subdirectory escapes its bind source")
    return replace(mount, backup_source=str(stable_source))
