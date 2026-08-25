"""Build wheel + sdist for minimax-remaining-mcp, bypassing setuptools' cleanup
(which chmods dist/.tmp-* directories on Windows and conflicts with the DSH
sandbox). Emits PEP 491-compliant artifacts in ./dist/.

Wheel filename MUST use underscores, not hyphens, in the distribution name
(pip's wheel installer parses hyphens as version/component separators and
rejects filenames with more than 6 dash-separated components).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import stat
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_PKG = ROOT / "src" / "minimax_remaining_mcp"
DIST = ROOT / "dist"
NAME = "minimax-remaining-mcp"  # PyPI name (with hyphens)
IMPORT_NAME = "minimax_remaining_mcp"  # Python import name (underscored)
VERSION = "0.1.4"


# ----------------------------------------------------------------------
# Files to include
# ----------------------------------------------------------------------

PACKAGE_FILES = [
    "__init__.py",
    "api.py",
    "browser.py",
    "config.py",
    "server.py",
    "storage.py",
    "window.py",
    "py.typed",
]

# Files to ship at the repo root (for sdist only — wheel has them in dist-info)
REPO_FILES = [
    "pyproject.toml",
    "README.md",
    "MANIFEST.in",
    "LICENSE",
]

DIST_INFO_FILES = [
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "RECORD",
    "LICENSE",
]


def _read(p: Path) -> bytes:
    return p.read_bytes()


def _sha256(data: bytes) -> str:
    return "sha256=" + base64.b64encode(hashlib.sha256(data).digest()).decode()


# ----------------------------------------------------------------------
# Metadata (PEP 621 / PEP 566)
# ----------------------------------------------------------------------

PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
README_MD = (ROOT / "README.md").read_text(encoding="utf-8")
LICENSE_TXT = (ROOT / "LICENSE").read_text(encoding="utf-8")


def _extract_pyproject(name: str) -> str:
    """Tiny extractor: only supports `key = "value"` (string) lines."""
    for line in PYPROJECT.splitlines():
        line = line.strip()
        if line.startswith(f"{name} ="):
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
    raise KeyError(name)


# ----------------------------------------------------------------------
# METADATA
# ----------------------------------------------------------------------

def build_metadata() -> bytes:
    summary = _extract_pyproject("description")
    authors_line = "yang-cc"
    metadata = (
        f'Metadata-Version: 2.1\n'
        f'Name: {NAME}\n'
        f'Version: {VERSION}\n'
        f'Summary: {summary}\n'
        f'Author: {authors_line}\n'
        f'License: MIT\n'
        f'Requires-Python: >=3.11\n'
        # PyPI defaults to reST — our README is Markdown, must declare this.
        f'Description-Content-Type: text/markdown\n'
        f'Classifier: Development Status :: 4 - Beta\n'
        f'Classifier: Intended Audience :: Developers\n'
        f'Classifier: Operating System :: OS Independent\n'
        f'Classifier: Programming Language :: Python :: 3\n'
        f'Classifier: Programming Language :: Python :: 3.11\n'
        f'Classifier: Programming Language :: Python :: 3.12\n'
        f'Classifier: Programming Language :: Python :: 3 :: Only\n'
        f'Classifier: Topic :: Software Development :: Libraries :: Python Modules\n'
        f'Classifier: Typing :: Typed\n'
        f'Requires-Dist: camoufox[geoip]>=0.5\n'
        f'Requires-Dist: fastmcp>=0.4\n'
        f'Requires-Dist: playwright>=1.40\n'
        f'Requires-Dist: requests>=2.30\n'
        f'Requires-Dist: pydantic>=2.0\n'
        f'Provides-Extra: dev\n'
        f'Requires-Dist: pytest>=7.0; extra == "dev"\n'
        f'Requires-Dist: pytest-asyncio>=0.21; extra == "dev"\n'
        f'Requires-Dist: ruff>=0.4; extra == "dev"\n'
        f'\n'
    )
    # Long description from README (PKG-INFO uses this convention)
    metadata += README_MD
    if not metadata.endswith("\n"):
        metadata += "\n"
    return metadata.encode("utf-8")


# ----------------------------------------------------------------------
# WHEEL
# ----------------------------------------------------------------------

WHEEL_METADATA = (
    'Wheel-Version: 1.0\n'
    'Generator: _build_publish.py (manual)\n'
    'Root-Is-Purelib: true\n'
    'Tag: py3-none-any\n'
).encode("utf-8")

ENTRY_POINTS = (
    '[console_scripts]\n'
    f'minimax-mcp = {IMPORT_NAME}.server:main\n'
).encode("utf-8")

TOP_LEVEL = (
    f'{IMPORT_NAME}\n'
).encode("utf-8")


# ----------------------------------------------------------------------
# Build wheel
# ----------------------------------------------------------------------

def build_wheel() -> Path:
    dist_info = f"{IMPORT_NAME}-{VERSION}.dist-info"
    out_path = DIST / f"{IMPORT_NAME}-{VERSION}-py3-none-any.whl"

    # Collect file records as we add them so we can write RECORD last.
    records: list[tuple[str, int, str]] = []

    def add(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
        zi = zipfile.ZipInfo(arcname)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = (0o644 & 0xFFFF) << 16
        # Pin mtime so the build is reproducible across runs.
        zi.date_time = (2026, 1, 1, 0, 0, 0)
        zf.writestr(zi, data)
        records.append((arcname, len(data), _sha256(data)))

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Package files
        for fname in PACKAGE_FILES:
            src = SRC_PKG / fname
            arc = f"{IMPORT_NAME}/{fname}"
            add(zf, arc, _read(src))

        # dist-info files (most, before RECORD)
        meta = build_metadata()
        add(zf, f"{dist_info}/METADATA", meta)
        add(zf, f"{dist_info}/WHEEL", WHEEL_METADATA)
        add(zf, f"{dist_info}/entry_points.txt", ENTRY_POINTS)
        add(zf, f"{dist_info}/top_level.txt", TOP_LEVEL)
        add(zf, f"{dist_info}/LICENSE", LICENSE_TXT.encode("utf-8"))

        # RECORD: list of (path, size, sha256). For RECORD itself, hash empty.
        record_lines = [f"{a},{s},{h}" for a, s, h in records]
        record_lines.append(f"{dist_info}/RECORD,,")
        record_payload = ("\n".join(record_lines) + "\n").encode("utf-8")
        add(zf, f"{dist_info}/RECORD", record_payload)

    return out_path


# ----------------------------------------------------------------------
# Build sdist
# ----------------------------------------------------------------------

def build_sdist() -> Path:
    # PyPI normalizes sdist filenames: the project-name component must use
    # underscores (the Python import form), not hyphens. The internal
    # top-level directory can use either form; we keep the PyPI name
    # (hyphens) for readability.
    archive_name = f"{NAME}-{VERSION}"  # internal dir: minimax-remaining-mcp-0.1.3/
    tar_name = f"{IMPORT_NAME}-{VERSION}.tar.gz"  # file: minimax_remaining_mcp-0.1.3.tar.gz
    out_path = DIST / tar_name

    # PKG-INFO is required by PyPI for sdist uploads. We reuse the same
    # metadata we emit into the wheel's METADATA so the two stay in sync.
    pkg_info_bytes = build_metadata()
    # PKG-INFO uses the same email-headers format as METADATA (PEP 566 /
    # 621). No special transformation needed.
    pkg_info_path_in_sdist = f"{archive_name}/PKG-INFO"

    members: list[tuple[Path, str]] = []
    for fname in REPO_FILES:
        members.append((ROOT / fname, f"{archive_name}/{fname}"))
    members.append((None, pkg_info_path_in_sdist))  # placeholder, see below
    # Whole src tree, minus egg-info and __pycache__
    for p in (ROOT / "src").rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT / "src")
        if rel.parts[0].endswith(".egg-info"):
            continue
        if "__pycache__" in rel.parts:
            continue
        members.append((p, f"{archive_name}/src/{rel.as_posix()}"))

    with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for entry in members:
            src, arc = entry
            data = pkg_info_bytes if src is None else src.read_bytes()
            info = tarfile.TarInfo(name=arc)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.mktime((2026, 1, 1, 0, 0, 0, 0, 0, 0)))
            info.type = tarfile.REGTYPE
            tf.addfile(info, io.BytesIO(data))

    return out_path


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> None:
    DIST.mkdir(exist_ok=True)
    # Wipe stale contents (including hidden dotfiles from failed builds).
    def _onerror(fn, path, exc_info):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        try:
            fn(path)
        except OSError:
            pass
    for p in list(DIST.iterdir()):
        try:
            if p.is_dir():
                shutil.rmtree(p, onerror=_onerror)
            else:
                p.unlink()
        except OSError:
            pass

    wheel = build_wheel()
    sdist = build_sdist()

    # Final chmod cleanup (Windows quirk: read-only attributes can stick).
    for p in DIST.iterdir():
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    print(f"Built wheel: {wheel.relative_to(ROOT)} ({wheel.stat().st_size} bytes)")
    print(f"Built sdist: {sdist.relative_to(ROOT)} ({sdist.stat().st_size} bytes)")


if __name__ == "__main__":
    main()