#!/usr/bin/env python3
"""Build the installable Calibre plugin ZIP without third-party dependencies."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


FILES = ('__init__.py', 'config.py', 'transport.py', 'ui.py', 'plugin-import-name-kindle_sftp.txt', 'README.md', 'LICENSE')


def build(destination=None):
    root = Path(__file__).resolve().parent
    target = Path(destination) if destination is not None else root / 'build' / 'KindleSFTP.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, 'w', compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (root / name).read_bytes())
    return target


if __name__ == '__main__':
    print(build())
