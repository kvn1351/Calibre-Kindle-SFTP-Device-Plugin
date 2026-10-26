#!/usr/bin/env python3
"""Build the installable Calibre plugin ZIP without third-party dependencies."""

import argparse
import platform
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


FILES = ('__init__.py', 'config.py', 'transport.py', 'broker.py', 'provision.py', 'ui.py', 'plugin-import-name-kindle_sftp.txt', 'README.md', 'LICENSE')


def build(destination=None, binaries=None):
    root = Path(__file__).resolve().parent
    target = Path(destination) if destination is not None else root / 'build' / 'KindleSFTP.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    if binaries is None:
        cargo_toml = root.parent / 'tailcatctl/Cargo.toml'
        if not cargo_toml.is_file():
            cargo_toml = root / 'kindle-link/Cargo.toml'
        subprocess.run(
            ['cargo', 'build', '--release', '--manifest-path', str(cargo_toml)],
            check=True,
        )
        machine = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
        built_bin = cargo_toml.parent / 'target/release/tailcatctl'
        if not built_bin.is_file():
            built_bin = cargo_toml.parent / 'target/release/kindle-link'
        if sys.platform == 'darwin' and built_bin.is_file():
            subprocess.run(['codesign', '-s', '-', '-f', str(built_bin)], check=False)
        binaries = {f'{sys.platform}-{machine}': built_bin}
    if not binaries:
        raise ValueError('At least one built tailcatctl binary is required.')
    with ZipFile(target, 'w', compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (root / name).read_bytes())
        for target_name, binary in sorted(binaries.items()):
            for prefix in ('bin/tailcatctl-', 'bin/kindle-link-'):
                entry = ZipInfo(prefix + target_name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100755 << 16
                archive.writestr(entry, Path(binary).read_bytes())
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binaries-dir', type=Path, help='Package prebuilt binaries without building Rust.')
    args = parser.parse_args()
    binaries = None
    if args.binaries_dir:
        binaries = {}
        for p in args.binaries_dir.iterdir():
            if p.is_file():
                name = p.name
                for pfx in ('tailcatctl-', 'kindle-link-'):
                    if name.startswith(pfx):
                        binaries[name[len(pfx):]] = p
    print(build(binaries=binaries))
