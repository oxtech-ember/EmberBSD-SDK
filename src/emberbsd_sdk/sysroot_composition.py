# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compose an application sysroot from pinned EmberBSD sets and a Ports runtime."""

import importlib.util
import hashlib
from pathlib import Path
import platform
import shutil
import tempfile

from . import inventory, jsonio, llvm_runtime, llvm_runtime_install, sysroot_transfer as transfer
from .errors import SDKError
from .target_toolchain import CLANG_BINARIES, validate_clang_identity
from .targets import target_definition


def release_inputs(kit, pin, target):
    """Read only the pinned set contract, not historical producer host bindings."""
    kit = Path(transfer._directory(kit))
    transfer._digest(pin)
    data = transfer._read(kit / 'manifest.json', transfer.MAX_MANIFEST)
    if hashlib.sha256(data).hexdigest() != pin:
        raise SDKError('release manifest SHA256 mismatch')
    manifest = jsonio.loads_json(data, max_bytes=transfer.MAX_MANIFEST)
    if (manifest.get('repository') != 'oxtech-ember/EmberBSD' or
            manifest.get('architecture') != target):
        raise SDKError('select matching EmberBSD release sets')
    revision = transfer._digest(manifest.get('source_commit'), 40)
    selected = {}
    for name in ('base.tar.xz', 'comp.tar.xz'):
        matches = [item for item in manifest.get('files', []) if item.get('name') == name]
        if len(matches) != 1:
            raise SDKError('release must contain one ' + name)
        item = matches[0]
        transfer._digest(item.get('sha256'))
        path = kit / name
        if (type(item.get('size')) is not int or path.stat().st_size != item['size'] or
                inventory.sha256_file(path) != item['sha256']):
            raise SDKError('release set size or SHA256 mismatch: ' + name)
        selected[name] = dict(size=item['size'], sha256=item['sha256'])
    try:
        old_runtime = manifest['build_profile']['sdk_receipt']['signature']['runtime']['manifest']
        llvm_runtime.validate_identity(old_runtime, target=target,
                                       required_components=llvm_runtime.FULL_COMPONENTS)
    except (KeyError, TypeError) as exc:
        raise SDKError('release lacks its LLVM runtime identity') from exc
    return kit, revision, selected, old_runtime


def _copy_base(base, destination, old_runtime):
    # Remove only the old runtime's declared aliases, in our disposable copy.
    aliases = llvm_runtime_install.validate_base_aliases(base, old_runtime)
    for name in aliases:
        (base / name).unlink()
    shutil.rmtree(base / 'usr/include/c++')
    destination.mkdir()
    # usr/share/emberbsd/ports names the Ports sources the base was built
    # from: Ports builtin checks (editline, elftoolchain) accept a base
    # library only with its marker, and refuse to build a second provider.
    for name in ('lib', 'usr/include', 'usr/lib', 'usr/share/legal', 'usr/share/emberbsd/ports'):
        source = base / name
        if source.exists():
            shutil.copytree(source, destination / name, symlinks=True)
    loader = destination / 'libexec/ld.elf_so'
    loader.parent.mkdir()
    shutil.copy2(base / 'libexec/ld.elf_so', loader, follow_symlinks=False)


def compose(kit, output, *, manifest_sha256, runtime_stage, ports_source, target, compiler,
            sdk_revision):
    """Export selected base files and a freshly verified runtime without rebuilding libc."""
    definition = target_definition(target)
    validate_clang_identity(compiler, definition['triplet'])
    transfer._digest(sdk_revision, 40)
    output = transfer._new_path(output)
    kit, revision, sets, old_runtime = release_inputs(kit, manifest_sha256, target)
    stage = Path(transfer._directory(runtime_stage))
    extractor = Path(ports_source) / 'profiles/common-build-tools/cross/sysroot_archive.py'
    spec = importlib.util.spec_from_file_location('emberbsd_ports_sysroot_archive', extractor)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='.sysroot-compose-', dir=Path(output).parent) as temporary:
        base, tree = Path(temporary) / 'base', Path(temporary) / 'sysroot'
        module.extract([kit / name for name in sets], base)
        runtime = llvm_runtime.validate_stage(stage, target=target,
            required_components=llvm_runtime.FULL_COMPONENTS, base=base)
        if runtime['base']['os_revision'] != revision or runtime['llvm_version'] != compiler['version']:
            raise SDKError('runtime differs from release base or selected compiler')
        _copy_base(base, tree, old_runtime)
        shutil.copytree(stage / runtime['prefix'], tree / runtime['prefix'], symlinks=True)
        llvm_runtime_install.install_base_aliases(tree, runtime)
        compact_compiler = dict(family='clang', version=compiler['version'], triplet=definition['triplet'],
            binaries={name: compiler['binaries'][name]['sha256'] for name in CLANG_BINARIES})
        sources = dict(os=revision, ports=runtime['recipe']['ports_revision'], sdk=sdk_revision)
        origin = dict(format='emberbsd-sysroot-composition', version=1, target=target, sources=sources,
            base=dict(manifest_sha256=manifest_sha256, sets=sets),
            extractor_sha256=inventory.sha256_file(extractor), compiler=compact_compiler,
            runtime_sha256=inventory.sha256_file(stage / 'runtime.json'),
            execution_acceptance='not-assessed')
        origin_path = tree / 'usr/share/emberbsd/sysroot-origin.json'
        origin_path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_json(origin_path, origin)
        rows = inventory.sysroot(tree, definition['elf'])
        prefix = runtime['prefix']
        record = dict(manifest=runtime, root_mode=rows[prefix]['mode'],
            inventory={name[len(prefix)+1:]: row for name, row in rows.items() if name.startswith(prefix + '/')})
        provenance = dict(sources=sources, build_host=platform.system().lower() + '-' + platform.machine(),
            compiler=compact_compiler, runtime_sha256=transfer._hash_json(record), base_cxx='libcxx',
            build_context_sha256=transfer._hash_json(origin), build_receipt_sha256=transfer._hash_json(origin))
        return transfer._export_tree(str(tree), output, target=target, rows=rows,
                                     provenance=provenance, runtime=record)
