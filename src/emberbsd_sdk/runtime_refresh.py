# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted incremental build support.
"""Describe runtime updates that can use the existing dependency planner."""

import hashlib
import json
import os
from pathlib import Path

from .errors import SDKError
from . import llvm_runtime, process


# The matcher remains in Ports.  This child isolates its imports and receives
# only structured metadata; Git blob names are passed as argv, never to a shell.
_RECIPE_BRIDGE = r'''
import importlib.util, json, pathlib, subprocess, sys
request = json.load(sys.stdin)
if set(request) != {'ports', 'git', 'previous', 'current', 'changed_paths', 'target', 'machine_arch'}:
    raise ValueError('invalid recipe bridge request')
ports = pathlib.Path(request['ports']).resolve(strict=True)
module_path = (ports / 'pkgsrc/lang/compiler-rt/files/runtime.py').resolve(strict=True)
if not module_path.is_relative_to(ports):
    raise ValueError('Ports matcher leaves selected source')
spec = importlib.util.spec_from_file_location('emberbsd_selected_ports_runtime', module_path)
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)
if getattr(owner, 'RECIPE_COMPAT_API', None) != 1:
    raise ValueError('unsupported Ports recipe matcher API')
old, new = request['previous'], request['current']
names = owner.builtins_recipe_blob_names(old, new)
if names is None:
    print(json.dumps(False))
    sys.exit(0)
def blobs(recipe):
    return {name: subprocess.run(
        [request['git'], '-C', str(ports), 'show', recipe['ports_revision'] + ':' + name],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        for name in names if name in recipe['files']}
accepted = owner.compatible_builtins_recipe(old, new, blobs(old), blobs(new),
    request['changed_paths'], request['target'], request['machine_arch'])
print(json.dumps(accepted))
'''


def compatible_recipe_transition(previous, current, changed_paths, *, ports, python, git, target, machine_arch, environment):
    request = dict(ports=ports, git=git, previous=previous, current=current,
                   changed_paths=changed_paths, target=target, machine_arch=machine_arch)
    result = process.capture([python, '-B', '-c', _RECIPE_BRIDGE],
                             env=process.git_environment(environment),
                             input=json.dumps(request, sort_keys=True).encode('utf-8'))
    try:
        answer = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise SDKError('invalid Ports recipe matcher response') from exc
    if type(answer) is not bool:
        raise SDKError('invalid Ports recipe matcher response')
    return answer


def bootstrap_update(before, after):
    """Rebind an unchanged bootstrap payload, refreshing moved header consumers."""
    try:
        if before.get('compiler_family') != 'clang' or after.get('compiler_family') != 'clang':
            return None
        llvm_binding = 'EMBER_LLVM_SOURCE='
        previous_source = [flag for flag in before['flags'] if flag.startswith(llvm_binding)]
        selected_source = [flag for flag in after['flags'] if flag.startswith(llvm_binding)]
        if (before['llvm_source'] != after['llvm_source'] or len(previous_source) != 1 or
                previous_source != selected_source):
            return None
        old, new = before['runtime']['manifest'], after['runtime']['manifest']
        for manifest in (old, new):
            llvm_runtime.validate_identity(manifest)
            if set(manifest['components']) != {'builtins', 'libunwind-static'}:
                return None
        # Compare every recorded producer input and payload digest. Repository
        # commits may advance without changing the selected base or recipe files.
        def comparable(manifest):
            return dict(manifest,
                        base={k: v for k, v in manifest['base'].items()
                              if k != 'os_revision'},
                        recipe={k: v for k, v in manifest['recipe'].items()
                                if k != 'ports_revision'})
        if comparable(old) != comparable(new):
            return None
        if ({k: v for k, v in before['runtime'].items() if k != 'manifest'} !=
                {k: v for k, v in after['runtime'].items() if k != 'manifest'}):
            return None
        routes = []
        roots = []
        for context, manifest in ((before, old), (after, new)):
            stage = Path(context['runtime_prefix'])
            if not stage.is_absolute():
                return None
            for part in reversed(Path(manifest['prefix']).parts):
                if stage.name != part:
                    return None
                stage = stage.parent
            selected = llvm_runtime.runtime_paths(str(stage), manifest)
            for binding, role in (('EMBER_EXTERNAL_LLVM_RUNTIME=', 'runtime-prefix'),
                                  ('EMBER_LLVM_RESOURCE_DIR=', 'llvm-resource'),
                                  ('EMBER_LIBUNWIND_ARCHIVE=', 'llvm-unwind')):
                if [flag for flag in context['flags'] if flag.startswith(binding)] != [binding + selected[role]]:
                    return None
            routes.append(selected)
            roots.append(str(stage))
        moved = before['runtime_prefix'] != after['runtime_prefix']
        unwind = new['artifacts']['libunwind']['static']
        if moved and not equivalent_unwind_binding(routes[0]['llvm-unwind'], routes[1]['llvm-unwind'],
                                                  roots[1], unwind, new['files'][unwind]):
            return None
        # Reuse compiled payload; only consumers whose dependency files retain
        # the former absolute include binding need the existing owner refresh.
        return dict(headers=(new['prefix'] + '/include/',) if moved else (), linked=())
    except (SDKError, KeyError, TypeError, AttributeError, ValueError):
        return None


def unchanged_full_update(before, after):
    """Rebind identical full runtime outputs after rebuilding their foundation.

    The base is a producer input, not an extra runtime payload. Its own headers
    and libraries are refreshed by OS make and the prepared-source planner.
    This case requires every runtime output and its ABI/source/recipe identity
    to remain identical; it does not relax the changed-runtime ABI checks.
    """
    try:
        if before.get('compiler_family') != 'clang' or after.get('compiler_family') != 'clang':
            return None
        selector = 'EMBER_LLVM_SOURCE='
        previous = [flag for flag in before['flags'] if flag.startswith(selector)]
        selected = [flag for flag in after['flags'] if flag.startswith(selector)]
        if before['llvm_source'] != after['llvm_source'] or len(previous) != 1 or previous != selected:
            return None
        old, new = before['runtime']['manifest'], after['runtime']['manifest']
        for manifest in (old, new):
            llvm_runtime.validate_identity(manifest, required_components=llvm_runtime.FULL_COMPONENTS)
            bootstrap = manifest['bootstrap']['files']
            expected = (manifest['artifacts']['builtins'], manifest['artifacts']['libunwind']['static'])
            if set(bootstrap) != set(expected):
                return None
        def comparable(manifest):
            return {key: (dict(files=value['files']) if key in ('recipe', 'bootstrap') else value)
                    for key, value in manifest.items() if key != 'base'}
        if comparable(old) != comparable(new):
            return None
        if ({key: value for key, value in before['runtime'].items() if key != 'manifest'} !=
                {key: value for key, value in after['runtime'].items() if key != 'manifest'}):
            return None
        for context, manifest in ((before, old), (after, new)):
            stage = Path(context['runtime_prefix'])
            if not stage.is_absolute():
                return None
            for part in reversed(Path(manifest['prefix']).parts):
                if stage.name != part:
                    return None
                stage = stage.parent
            routes = llvm_runtime.runtime_paths(str(stage), manifest)
            for binding, role in (('EMBER_EXTERNAL_LLVM_RUNTIME=', 'runtime-prefix'),
                                  ('EMBER_LLVM_RESOURCE_DIR=', 'llvm-resource')):
                if [flag for flag in context['flags'] if flag.startswith(binding)] != [binding + routes[role]]:
                    return None
        moved = before['runtime_prefix'] != after['runtime_prefix']
        return dict(headers=(new['prefix'] + '/include/', 'usr/include/c++/') if moved else (),
                    linked=('libc++.so', 'libc++abi.so') if moved else ())
    except (SDKError, KeyError, TypeError, AttributeError, ValueError):
        return None


def cpp_update(before, after):
    """Return header/link inputs to refresh, or None for incompatible changes.

    Compiler and other build options are compared by the caller. The current
    runtime has already passed its owner verifier. Compare its recorded files,
    without reading or hashing either installed tree again.
    """
    try:
        if before.get('compiler_family') != 'clang' or after.get('compiler_family') != 'clang':
            return None
        old, new = before['runtime']['manifest'], after['runtime']['manifest']
        identity = ('format', 'version', 'compiler_family', 'llvm_version', 'components',
                    'target', 'triplet', 'machine_arch', 'elf_machine', 'prefix',
                    'source', 'artifacts', 'symlinks')
        if any(old[key] != new[key] for key in identity):
            return None
        if (old['format'] != 'emberbsd-llvm-runtime' or old['version'] != 1 or
                old['compiler_family'] != 'clang'):
            return None
        # An unrelated OS commit does not change the runtime's target C ABI.
        if ({k: v for k, v in old['base'].items() if k != 'os_revision'} !=
                {k: v for k, v in new['base'].items() if k != 'os_revision'}):
            return None
        headers = new['artifacts']['cxx_headers'] + '/'
        if headers != new['prefix'] + '/include/c++/v1/':
            return None
        shared = {new['artifacts'][name]['shared'] for name in ('libcxx', 'libcxxabi')}
        changed = {name for name in old['files'].keys() | new['files'].keys()
                   if old['files'].get(name) != new['files'].get(name)}
        if any(name not in shared and not name.startswith(headers) for name in changed):
            # Builtins, unwinder, static archives and other runtime inputs stay
            # unchanged; this is not a compiler or general runtime ABI switch.
            return None
        if changed or before['runtime_prefix'] != after['runtime_prefix']:
            # A moved prefix also needs new dependency files: otherwise a later
            # header update could remain hidden behind old absolute .d paths.
            # The optional base C++ installation copies these headers to
            # usr/include/c++; dependency files may use either public route.
            return dict(headers=(headers, 'usr/include/c++/'),
                        linked=('libc++.so', 'libc++abi.so'))
        return dict(headers=(), linked=())
    except (KeyError, TypeError, AttributeError):
        return None


def builtins_update(before, after, recipe_guard=None):
    """Identify an unchanged-ABI runtime update limited to builtins and C++ DSOs."""
    try:
        if before.get('compiler_family') != 'clang' or after.get('compiler_family') != 'clang':
            return None
        old, new = before['runtime']['manifest'], after['runtime']['manifest']
        identity = ('format', 'version', 'compiler_family', 'llvm_version', 'components',
                    'target', 'triplet', 'machine_arch', 'elf_machine', 'prefix',
                    'source', 'artifacts', 'symlinks')
        if any(old[key] != new[key] for key in identity):
            return None
        if (old['format'] != 'emberbsd-llvm-runtime' or old['version'] != 1 or
                old['compiler_family'] != 'clang' or not old['triplet'].endswith('--netbsd')):
            return None
        if ({k: v for k, v in old['base'].items() if k != 'os_revision'} !=
                {k: v for k, v in new['base'].items() if k != 'os_revision'}):
            return None
        builtins = new['artifacts']['builtins']
        unwind = new['artifacts']['libunwind']['static']
        old_boot, new_boot = old['bootstrap']['files'], new['bootstrap']['files']
        if (set(old_boot) != {builtins, unwind} or set(new_boot) != {builtins, unwind} or
                old_boot[unwind] != new_boot[unwind] or
                old_boot[builtins] != old['files'][builtins] or
                new_boot[builtins] != new['files'][builtins]):
            return None
        cxx = {new['artifacts'][name]['shared'] for name in ('libcxx', 'libcxxabi')}
        if not ({builtins} | cxx) <= old['files'].keys() or not ({builtins} | cxx) <= new['files'].keys():
            return None
        changed = {name for name in old['files'].keys() | new['files'].keys()
                   if old['files'].get(name) != new['files'].get(name)}
        if builtins not in changed or not changed <= {builtins} | cxx:
            return None
        if old['recipe'] != new['recipe'] and (
                recipe_guard is None or not recipe_guard(old['recipe'], new['recipe'])):
            return None
        # Shared DSO contents can change, but ABI, headers, static archives,
        # source tree and SONAME links must remain compatible; consumers stay.
        return dict(builtins=builtins)
    except (KeyError, TypeError, AttributeError):
        return None


def equivalent_unwind_binding(previous, selected, selected_stage, artifact, digest):
    """Accept only a relocated, byte-identical selected bootstrap unwind."""
    try:
        old, new, stage, relative = map(Path, (previous, selected, selected_stage, artifact))
        if (not old.is_absolute() or not new.is_absolute() or not stage.is_absolute() or
                relative.is_absolute() or '..' in relative.parts or
                not old.is_file() or old.is_symlink() or not new.is_file() or new.is_symlink() or
                new != stage / relative or not isinstance(digest, str)):
            return False
        return all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path in (old, new))
    except (OSError, TypeError, ValueError):
        return False


def changed_builtins(old_source, new_source):
    """Compare the selected compiler-rt builtins source subset."""
    prefix = Path('compiler-rt/lib/builtins')
    sources = []
    def walk_error(error):
        raise error
    for source in (old_source, new_source):
        root = Path(source)
        base = root / prefix
        if not root.is_absolute() or not base.is_dir() or not base.resolve().is_relative_to(root.resolve()):
            raise SDKError('invalid runtime source root')
        files = {}
        for directory, subdirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            if any((parent / name).is_symlink() for name in subdirs + names):
                raise SDKError('symlink in runtime source inventory')
            for name in names:
                path = parent / name
                rel = path.relative_to(root).as_posix()
                if not path.is_file():
                    raise SDKError('non-file in runtime source inventory')
                if rel != 'llvm-source.json':
                    files[rel] = path
        sources.append(files)
    key_prefix = str(prefix) + '/'
    builtins = [{name for name in files if name.startswith(key_prefix)} for files in sources]
    if builtins[0] != builtins[1]:
        raise SDKError('builtins source inventory changed')
    if sources[0].keys() != sources[1].keys():
        raise SDKError('runtime source inventory changed')
    changed = [name for name in builtins[0]
               if sources[0][name].read_bytes() != sources[1][name].read_bytes()]
    if not changed:
        raise SDKError('builtins changed but no source differences found')
    if any(Path(name).suffix not in ('.c', '.h', '.inc') for name in changed):
        raise SDKError('unsupported builtins source change')
    # Owner-verified trees must agree outside builtins except the scoped,
    # Darwin-only codesign command, which does not run for this NetBSD target.
    darwin = 'compiler-rt/cmake/Modules/AddCompilerRT.cmake'
    for name, old_file in sources[0].items():
        if name.startswith(str(prefix) + '/'):
            continue
        before = old_file.read_bytes()
        after = sources[1][name].read_bytes()
        if before == after:
            continue
        old_cmd = b'COMMAND ' + bytes((36,)) + b'{CODESIGN} --sign - ' + bytes((36,)) + b'{EXTRA_CODESIGN_ARGUMENTS}'
        new_cmd = b'COMMAND ' + bytes((36,)) + b'{CODESIGN} --force --sign - ' + bytes((36,)) + b'{EXTRA_CODESIGN_ARGUMENTS}'
        guard = b'if (APPLE AND NOT CMAKE_LINKER MATCHES'
        guard_at, command_at = before.find(guard), before.find(old_cmd)
        depth = 0
        if guard_at >= 0 and command_at > guard_at:
            for line in before[guard_at:command_at].splitlines():
                word = line.strip().lower()
                if word.startswith((b'if(', b'if (')):
                    depth += 1
                elif word.startswith((b'endif(', b'endif (')):
                    depth -= 1
                if depth <= 0:
                    break
        if (name != darwin or before.count(old_cmd) != 1 or depth <= 0 or
                before.replace(old_cmd, new_cmd, 1) != after):
            raise SDKError('unrelated runtime source changed: ' + name)
    return sorted(changed)
