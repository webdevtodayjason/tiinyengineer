"""Minimal build backend so an archive never needs a downloaded build package."""

from __future__ import annotations

import base64
import csv
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import zipfile


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    del config_settings, metadata_directory
    name = "tiinyengineer-0.1.0-py3-none-any.whl"
    destination = Path(wheel_directory) / name
    package = Path(__file__).parent / "tiinyengineer"
    dist = "tiinyengineer-0.1.0.dist-info"
    files: dict[str, bytes] = {}
    for source in package.rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            files[source.relative_to(package.parent).as_posix()] = source.read_bytes()
    files[f"{dist}/METADATA"] = b"Metadata-Version: 2.1\nName: tiinyengineer\nVersion: 0.1.0\nRequires-Python: >=3.11\n"
    files[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: tiinyengineer\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist}/entry_points.txt"] = b"[console_scripts]\ntiinyengineer=tiinyengineer.__main__:main\n"
    rows = []
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            archive.writestr(path, body)
            digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
            rows.append((path, f"sha256={digest}", str(len(body))))
        record = f"{dist}/RECORD"
        rows.append((record, "", ""))
        with tempfile.TemporaryFile(mode="w+", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)
            handle.seek(0)
            archive.writestr(record, handle.read())
    return name


def build_sdist(sdist_directory: str, config_settings=None) -> str:
    del config_settings
    name = "tiinyengineer-0.1.0.tar.gz"
    root = Path(__file__).parent
    with tarfile.open(Path(sdist_directory) / name, "w:gz") as archive:
        for item in ("pyproject.toml", "_custom_build.py", "README.md", "tiinyengineer"):
            source = root / item
            archive.add(source, arcname=f"tiinyengineer-0.1.0/{item}")
    return name


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    del config_settings
    target = Path(metadata_directory) / "tiinyengineer-0.1.0.dist-info"
    target.mkdir(parents=True, exist_ok=True)
    (target / "METADATA").write_text("Metadata-Version: 2.1\nName: tiinyengineer\nVersion: 0.1.0\n", encoding="utf-8")
    return target.name
