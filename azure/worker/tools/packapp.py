#!/usr/bin/env python3

import argparse
import os
import os.path
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import distlib.scripts
import distlib.wheel


_platform_map = {
    'linux': 'manylinux1_x86_64',
    'windows': 'win_amd64',
}

_wheel_file_pattern = r"""
    ^{namever}
    ((-(?P<build>\d[^-]*?))?-(?P<pyver>.+?)-(?P<abi>.+?)-(?P<plat>.+?)
    \.whl)$
"""


def die(msg):
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


def main(argv=sys.argv[1:]):
    args = parse_args(argv)
    if not args.no_deps:
        find_and_build_deps(args)


def find_and_build_deps(args):
    app_path = pathlib.Path(args.path)
    req_txt = app_path / 'requirements.txt'

    if not req_txt.exists():
        die('missing requirements.txt file.  '
            'If you do not have any requirements, please pass --no-deps.')

    packages = []

    # First, we need to figure out the complete list of dependencies
    # without actually installing them.  Use straight `pip download`
    # for that.
    with tempfile.TemporaryDirectory(prefix='azureworker') as td:
        subprocess.run([
            'pip', 'download', '-r', str(req_txt), '--dest', td
        ], check=True)

        files = os.listdir(td)

        for filename in files:
            m = re.match(r'^(?P<name>.+?)-(?P<ver>.*?)-.*\.whl$', filename)
            if m:
                # This is a wheel.
                packages.append((m.group('name'), m.group('ver')))
            else:
                # This is a sdist.
                m = re.match(r'^(?P<namever>.+)(\.tar\.gz|\.tgz|\.zip)$',
                             filename)
                if m:
                    name, _, ver = m.group('namever').rpartition('-')
                    if name and ver:
                        packages.append((name, ver))

    # Now that we know all dependencies, download or build wheels
    # for them for the correct platform and Python verison.
    with tempfile.TemporaryDirectory(prefix='azureworker') as td:
        for name, ver in packages:
            ensure_wheel(name, ver, args=args, dest=td)

        with tempfile.TemporaryDirectory(prefix='azureworkervenv') as venv:
            venv = pathlib.Path(venv)
            pyver = args.python_version
            python = f'python{pyver[0]}.{pyver[1]}'

            if args.platform == 'windows':
                sp = venv / 'Lib' / 'site-packages'
                headers = venv / 'Include'
                scripts = venv / 'Scripts'
                data = venv
            elif args.platform == 'linux':
                sp = venv / 'lib' / python / 'site-packages'
                headers = venv / 'include' / 'site' / python
                scripts = venv / 'bin'
                data = venv
            else:
                die(f'unsupported platform: {args.platform}')

            maker = distlib.scripts.ScriptMaker(None, None)

            for filename in os.listdir(td):
                if not filename.endswith('.whl'):
                    continue

                wheel = distlib.wheel.Wheel(os.path.join(td, filename))

                paths = {
                    'prefix': venv,
                    'purelib': sp,
                    'platlib': sp,
                    'headers': headers / wheel.name,
                    'scripts': scripts,
                    'data': data
                }

                for dn in paths.values():
                    os.makedirs(dn, exist_ok=True)

                # print(paths, maker)
                wheel.install(paths, maker)

            for root, dirs, files in os.walk(venv):
                for file in files:
                    src = os.path.join(root, file)
                    rpath = app_path / '.python_packages/' / \
                        os.path.relpath(src, venv)
                    dir_name, _ = os.path.split(rpath)
                    os.makedirs(dir_name, exist_ok=True)
                    shutil.copyfile(src, rpath)


def ensure_wheel(name, version, args, dest):
    cmd = [
        'pip', 'download', '--no-deps', '--only-binary', ':all:',
        '--platform', _platform_map.get(args.platform),
        '--python-version', args.python_version,
        '--implementation', 'cp',
        '--abi', f'cp{args.python_version}m',
        '--dest', dest,
        f'{name}=={version}'
    ]

    pip = subprocess.run(cmd)
    if pip.returncode != 0:
        # No wheel for this package for this platform or Python version.
        if not build_independent_wheel(name, version, args, dest):
            build_binary_wheel(name, version, args, dest)


def build_independent_wheel(name, version, args, dest):
    with tempfile.TemporaryDirectory(prefix='azureworker') as td:
        cmd = [
            'pip', 'wheel', '--no-deps', '--no-binary', ':all:',
            '--wheel-dir', td,
            f'{name}=={version}'
        ]

        # First, try to build it as an independent wheel.
        pip = subprocess.run(cmd)
        if pip.returncode != 0:
            return False

        wheel_re = _wheel_file_pattern.format(namever=f'{name}-{version}')

        for filename in os.listdir(td):
            m = re.match(wheel_re, filename, re.VERBOSE)
            if m:
                abi = m.group('abi')
                platform = m.group('plat')

                if abi == 'none' and platform == 'any':
                    # This is a universal wheel.
                    shutil.move(os.path.join(td, filename), dest)
                    return True

                break

        return False


def build_binary_wheel(name, version, args, dest):
    if args.platform != 'linux':
        die('packapp can only build binary dependencies for Linux targets')


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', type=str)
    parser.add_argument('--python-version', type=str)
    parser.add_argument('--no-deps', default=False, action='store_true')
    parser.add_argument('path', type=str,
                        help='Path to a function app to pack.')

    args = parser.parse_args(argv)
    if not args.platform:
        die('missing required argument: --platform')

    if not args.python_version:
        die('missing required argument: --python-version')

    return args


if __name__ == '__main__':
    main()

