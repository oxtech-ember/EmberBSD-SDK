# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Single-file delivery of the existing portable sysroot transfer format."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from . import application_toolchain, jsonio, publication, sysroot_transfer, target_toolchain
from .errors import SDKError
from .host_prefix import load_host_prefix
from .package_install import target_sysroot
from .targets import target_definition

RECEIPT = '.ember-sdk-artifact.json'
MEMBERS = ('manifest.json', 'READY', 'payload.tar.gz')


def package(bundle, output, *, target, manifest_sha256):
    """Wrap a verified transfer, without recompressing its payload."""
    bundle, output = Path(bundle), Path(output)
    manifest = sysroot_transfer._manifest(str(bundle), target, manifest_sha256,
                                         sysroot_transfer.MAX_BYTES)
    sysroot_transfer._check_payload(str(bundle / 'payload.tar.gz'), manifest['payload'])
    with output.open('xb') as stream, tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name in MEMBERS:
            path = bundle / name
            info = tarfile.TarInfo(name)
            info.size, info.mode = path.stat().st_size, 0o644
            with path.open('rb') as source:
                archive.addfile(info, source)
    return dict(path=str(output), target=target, unpacked_size=manifest['unpacked_bytes'],
                manifest_sha256=manifest_sha256)


@contextmanager
def open_bundle(archive_path, *, target, max_bytes):
    """Read only the three fixed regular members; never extract archive paths."""
    with tempfile.TemporaryDirectory(prefix='ember-sysroot-') as directory:
        bundle = Path(directory).resolve()
        with tarfile.open(archive_path, 'r|') as archive:
            for name in MEMBERS:
                member = archive.next()
                limit = (max_bytes + sysroot_transfer.MAX_MANIFEST if name == 'payload.tar.gz'
                         else 65 if name == 'READY' else sysroot_transfer.MAX_MANIFEST)
                if (member is None or member.name != name or not member.isfile() or
                        member.size < 1 or member.size > limit):
                    raise SDKError('invalid sysroot package member: ' + name)
                with archive.extractfile(member) as source, (bundle / name).open('xb') as destination:
                    shutil.copyfileobj(source, destination)
            if archive.next() is not None:
                raise SDKError('unexpected sysroot package member')
        pin = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
        manifest = sysroot_transfer._manifest(str(bundle), target, pin, max_bytes)
        sysroot_transfer._check_payload(str(bundle / 'payload.tar.gz'), manifest['payload'])
        yield bundle, pin, manifest


def selection(release, config, cache, target):
    """Select exactly one published target/ABI, independently of the host."""
    definition = target_definition(target)
    candidates = [a for a in release['artifacts'] if a['kind'] == 'sysroot' and
                  a['target']['arch'] == definition['arch'] and
                  a['target']['triplet'] == definition['triplet']]
    configured = [c for c in config['repositories'].values() if c.get('target') and
                  c['target']['triplet'] == definition['triplet']]
    if any(not target_sysroot(c) for c in configured):
        raise SDKError('a live target installation cannot receive an SDK sysroot')
    if configured:
        candidates = [a for a in candidates if a['target'] in [c['target'] for c in configured]]
    if len(candidates) != 1:
        raise SDKError(f'{target}: expected one published sysroot for the selected ABI; found {len(candidates)}')
    artifact = candidates[0]
    if artifact['depends']:
        raise SDKError('sysroot must contain its complete target runtime; external artifact dependencies are unsupported')
    matching = {c['sysroot'] for c in configured if c['target'] == artifact['target']}
    if len(matching) > 1:
        raise SDKError('selected target repositories must share one sysroot')
    if matching:
        destination = Path(matching.pop())
    else:
        root = Path(config['host_prefix']).resolve() / 'sysroots'
        if root.is_symlink():
            raise SDKError('SDK sysroot parent must not be a symlink')
        destination = root / target
    receipt = destination / RECEIPT
    identity = dict(sha256=artifact['sha256'], target=artifact['target'])
    if destination.is_symlink():
        raise SDKError('SDK sysroot destination must not be a symlink')
    destination = destination.resolve()
    if destination.exists():
        if not receipt.is_file() or jsonio.read_json(receipt) != identity:
            raise SDKError('another sysroot is installed; use a coherent component update before replacing it')
        action = 'installed'
    else:
        action = 'cache' if cache.path(artifact).exists() else 'download'
    return dict(artifact=artifact, destination=str(destination), target=target, action=action)


def check(archive, selected):
    item = selected['artifact']
    with open_bundle(archive, target=selected['target'], max_bytes=item['unpacked_size']) as (_, _, manifest):
        if manifest['unpacked_bytes'] != item['unpacked_size'] or manifest['version'] != 2:
            raise SDKError('sysroot package size or LLVM runtime differs from the signed release')
        return manifest


def install(archive, selected):
    destination = Path(selected['destination'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    item = selected['artifact']
    with open_bundle(archive, target=selected['target'], max_bytes=item['unpacked_size']) as (bundle, pin, manifest):
        if manifest['unpacked_bytes'] != item['unpacked_size'] or manifest['version'] != 2:
            raise SDKError('sysroot package size or LLVM runtime differs from the signed release')
        with tempfile.TemporaryDirectory(prefix='.sysroot-install-', dir=destination.parent) as directory:
            tree = Path(directory).resolve() / 'tree'
            sysroot_transfer.import_sysroot(bundle, tree, target=selected['target'], manifest_sha256=pin,
                                           max_bytes=item['unpacked_size'])
            jsonio.write_json(tree / RECEIPT, dict(sha256=item['sha256'], target=item['target']))
            publication.publish_directory_noreplace(str(tree), str(destination))


def bind_compiler(config, selected):
    """Expose the SDK's operation-aware compiler commands after installation."""
    prefix = Path(config['host_prefix'])
    if not (prefix / 'bin/clang').is_file():
        return  # A sysroot can be acquired before its compiler.
    target = selected['target']
    triplet = target_definition(target)['triplet']
    destination = Path(config['state_dir']) / 'toolchains' / target
    aliases = {'cc': 'cc', 'cxx': 'c++', 'cpp': 'cpp', 'as': 'as', 'ld': 'ld'}
    links = {role: prefix / 'bin' / (triplet + '-' + suffix) for role, suffix in aliases.items()}
    for role, link in links.items():
        if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != (destination / role).resolve()):
            raise SDKError('compiler command already belongs to another provider: ' + str(link))
    if not destination.exists():
        native = load_host_prefix(prefix, required_roles=('python',))
        compiler = target_toolchain.clang_compiler(prefix, triplet, host=native.host,
                                                  env={'PATH': native.path, 'LC_ALL': 'C'})
        runtime = application_toolchain.load_clang_runtime(selected['destination'], target=target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        application_toolchain.bind_clang_application_commands(destination, compiler, selected['destination'],
            runtime, python=native.tool('python'), include_auxiliary=True)
    for role, link in links.items():
        if not os.path.lexists(link):
            link.symlink_to(os.path.relpath(destination / role, link.parent))
