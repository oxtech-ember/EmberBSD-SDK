# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""The OS inputs that reach the LLVM bootstrap stages, as Git object ids.

A stage set (headers and base) is reused for a later OS commit when the Git
blob ids of its inputs are unchanged. The inputs are taken from the stage's
own build, not from a list of consumers:

* every OS directory with outputs in the stage object trees, and every
  directory named by a compiler dependency file there;
* for the OS host tools built into those trees, only the tools the stage's
  build log runs (matched by the installed binary), their host libraries
  and bmake. A tool that the stage never runs, such as config(1), cannot
  change the stage outputs;
* the shared build rules: build.sh, the top Makefile, share/mk, etc/mtree,
  tools/, every shared makefile fragment, and every header and makefile
  below the trees that install headers.

The direct files of each selected directory and of its parent directories
are included, so a new file in a used directory changes the key.
"""

import hashlib
import json
import os
import re
import stat

from . import jsonio, paths
from .errors import SDKError

FORMAT = 'emberbsd-llvm-stage-inputs'
VERSION = 1
RECORD = 'inputs.json'
PHASES = ('headers', 'base')
FIXED_FILES = ('build.sh', 'Makefile')
FIXED_TREES = ('share/mk', 'etc/mtree', 'tools')
HEADER_ROOTS = ('common', 'crypto', 'external', 'gnu', 'include', 'lib', 'sys')
# bmake runs every rule; it is started through the nbmake wrapper script.
ALWAYS_TOOLS = ('tools/make',)
_DEPENDENCY_NAMES = re.compile(r'(?:\.d|\.depend(?:\..*)?)\Z')
_PROGRAM = re.compile(r'[A-Za-z0-9_.+-]+')
# build.sh writes the nbmake-MACHINE wrapper; it runs bmake from tools/make.
WRAPPERS = (('nbmake-', 'tools/make'),)


def shared_makefile(name):
    return name == 'Makefile.inc' or name.startswith('Makefile.') or name.endswith('.mk')


def _relative(path, root):
    path = os.path.normpath(path)
    if path == root or not path.startswith(root + '/'):
        return None
    return path[len(root) + 1:]


def dependency_files(directory, root):
    """Return OS-relative files named by the dependency files of one directory."""
    found = set()
    for name in sorted(os.listdir(directory)):
        if _DEPENDENCY_NAMES.search(name) is None:
            continue
        path = os.path.join(directory, name)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            continue
        with open(path, 'rb') as handle:
            text = handle.read().decode('utf-8', 'surrogateescape')
        for token in text.replace('\\\n', ' ').split():
            token = token.rstrip(':')
            if token.startswith('/'):
                relative = _relative(token, root)
                if relative is not None:
                    found.add(relative)
    return found


def _executed(line, prefix):
    """Return the TOOLDIR/bin programs one log line runs, not ones it installs."""
    text = line.decode('utf-8', 'surrogateescape').strip()
    # bmake target banners, build.sh progress notes and wrapper creation name
    # a program without running it.
    if text.startswith(('---', '#', '===>')) or 'makewrapper:' in text:
        return set()
    tokens = text.split()
    if tokens and tokens[0].startswith(prefix) and tokens[0].endswith('install'):
        # An install command runs the installer; its last operand is the
        # destination being created.
        tokens = tokens[:-1]
    found = set()
    for token in tokens:
        start = token.find(prefix)
        while start >= 0:
            match = _PROGRAM.match(token, start + len(prefix))
            if match is not None:
                found.add(match.group(0))
            start = token.find(prefix, start + len(prefix))
    return found


def invoked_tools(log, tools):
    """Return the TOOLDIR/bin programs a stage build log runs."""
    prefix = os.path.join(tools, 'bin') + '/'
    names = set()
    with open(log, 'rb') as handle:
        for line in handle:
            names.update(_executed(line, prefix))
    return names


def log_sources(log, root):
    """Return OS-relative paths that commands in a stage build log read.

    Rules also read files that no dependency file lists, such as an awk script
    found through .PATH in another directory; the log names them in full.
    """
    prefix = root.rstrip('/') + '/'
    found = set()
    with open(log, 'rb') as handle:
        for line in handle:
            text = line.decode('utf-8', 'surrogateescape')
            start = text.find(prefix)
            while start >= 0:
                end = start
                while end < len(text) and not text[end].isspace() and text[end] not in '\'";:|<>()`,=':
                    end += 1
                relative = _relative(text[start:end], root.rstrip('/'))
                if relative is not None:
                    found.add(relative)
                start = text.find(prefix, end)
    return found


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _regular_files(directory):
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            yield name, path, info


def phase_inputs(objects, root, tools, log):
    """Collect one phase's used directories from its object tree and log."""
    if paths.entry(objects) is None or paths.entry(log) is None:
        raise SDKError(f'LLVM stage inputs need the stage objects and build log: {objects}')
    invoked = {}
    for name in sorted(invoked_tools(log, tools)):
        program = os.path.join(tools, 'bin', name)
        if any(name.startswith(wrapper) for wrapper, _ in WRAPPERS):
            continue
        if paths.entry(program) is not None and stat.S_ISREG(os.lstat(program).st_mode):
            invoked[name] = _sha256(program)
    hashes = set(invoked.values())
    sizes = {os.lstat(os.path.join(tools, 'bin', name)).st_size for name in invoked}
    directories, used_tools, matched = set(), set(), set()
    tool_dirs = {}
    for current, subdirectories, _ in os.walk(objects):
        subdirectories.sort()
        relative = os.path.relpath(current, objects)
        relative = '' if relative == '.' else relative
        files = list(_regular_files(current))
        if not files or not relative:
            continue
        if relative == 'tools' or relative.startswith('tools/'):
            tool_dirs[relative] = (current, files)
            continue
        directories.add(relative)
        directories.update(os.path.dirname(name) for name in dependency_files(current, root))
    for relative, (current, files) in sorted(tool_dirs.items()):
        used = relative in ALWAYS_TOOLS or relative in {tool for _, tool in WRAPPERS} or any(name.endswith('.a') for name, _, _ in files)
        for name, path, info in files:
            # Scripts such as genassym are installed from a generated, not
            # executable, file; compare content only.
            if info.st_size and info.st_size in sizes:
                value = _sha256(path)
                if value in hashes:
                    used = True
                    matched.add(value)
        if used:
            used_tools.add(relative)
    unmatched = sorted(name for name, value in invoked.items() if value not in matched)
    if unmatched:
        # A program without its object directory: keep every tool's inputs.
        used_tools = set(tool_dirs)
    unused = set()
    for relative, (current, _) in tool_dirs.items():
        owned = {relative} | {os.path.dirname(name) for name in dependency_files(current, root)}
        if relative in used_tools:
            directories.update(owned)
        else:
            unused.update(owned)
    # The stage also builds the host tools; the log lines that compile a tool
    # the stage never runs name only that tool's sources.
    unused -= directories
    for relative in log_sources(log, root):
        # A logged path may name a directory (cd, -I) or a file.
        if not os.path.isdir(os.path.join(root, relative)):
            relative = os.path.dirname(relative)
        if relative not in unused:
            directories.add(relative)
    directories.discard('')
    return dict(directories=directories, tools=sorted(used_tools), invoked=sorted(invoked),
                unmatched=unmatched)


def tree_rows(git, root, commit):
    """Return (mode, type, object id, path) rows of a commit, without trees."""
    output = git(root, 'ls-tree', '-r', '-z', '--full-tree', commit)
    rows = []
    for entry in output.split('\0'):
        if not entry:
            continue
        meta, _, path = entry.partition('\t')
        mode, kind, oid = meta.split(' ')
        rows.append((mode, kind, oid, path))
    return rows


def selected_rows(rows, directories):
    selected = set(directories)
    for directory in directories:
        while directory:
            directory = os.path.dirname(directory)
            selected.add(directory)
    result = []
    for row in rows:
        path = row[3]
        parent, name = os.path.split(path)
        top = path.split('/', 1)[0]
        if (parent in selected or path in FIXED_FILES or
                any(path.startswith(tree + '/') for tree in FIXED_TREES) or shared_makefile(name) or
                (top in HEADER_ROOTS and (name.endswith('.h') or name.startswith('Makefile')))):
            result.append(row)
    return result


def inputs_digest(rows, directories):
    lines = ''.join(f'{mode} {kind} {oid}\t{path}\n'
                    for mode, kind, oid, path in selected_rows(rows, directories))
    return hashlib.sha256(lines.encode('utf-8', 'surrogateescape')).hexdigest()


def stage_key(target, llvm_source, compiler, digest):
    material = dict(format='emberbsd-llvm-stage-key', version=2, target=target,
                    inputs=digest, llvm_source=llvm_source, compiler=compiler)
    encoded = json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def make_record(receipts, git, target):
    """Record the inputs of a completed stage set from its phase receipts."""
    if set(receipts) != set(PHASES):
        raise SDKError('LLVM stage inputs need the headers and base receipts')
    first = receipts['headers']
    for phase, receipt in receipts.items():
        if (receipt.get('format') != 'emberbsd-llvm-os-stage' or receipt.get('phase') != phase or
                receipt.get('qualification') != 'built-and-inspected'):
            raise SDKError('LLVM stage inputs need completed stage receipts')
        if receipt['paths']['os'] != first['paths']['os']:
            raise SDKError('LLVM stage phases use different OS checkouts')
        if receipt['sources']['os']['commit'] != first['sources']['os']['commit']:
            raise SDKError('LLVM stage phases were built from different OS commits')
    root = first['paths']['os']
    commit = first['sources']['os']['commit']
    directories, phases = set(), {}
    for phase in PHASES:
        receipt = receipts[phase]
        log = os.path.join(receipt['paths']['destination'], '.prebuild-sdk-build.log')
        found = phase_inputs(receipt['paths']['objects'], root, receipt['paths']['tools'], log)
        directories |= found.pop('directories')
        phases[phase] = found
    directories = sorted(directories)
    base = receipts['base']['signature']
    digest = inputs_digest(tree_rows(git, root, commit), directories)
    return dict(format=FORMAT, version=VERSION, target=target, os_commit=commit,
                directories=directories, phases=phases, inputs=digest,
                key=stage_key(target, base['llvm_source'], base['compiler'], digest))


def read_record(path):
    record = jsonio.read_json(path)
    if (not isinstance(record, dict) or record.get('format') != FORMAT or
            type(record.get('version')) is not int or record['version'] != VERSION or
            not isinstance(record.get('directories'), list) or
            not all(isinstance(value, str) and value and not value.startswith('/') and
                    '..' not in value.split('/') for value in record['directories']) or
            not isinstance(record.get('key'), str) or re.fullmatch('[0-9a-f]{64}', record['key']) is None or
            not isinstance(record.get('os_commit'), str) or
            re.fullmatch('[0-9a-f]{40}', record['os_commit']) is None):
        raise SDKError(f'invalid LLVM stage input record: {path}')
    return record


def write_record(path, record):
    """Publish a complete record once; an existing record must be identical."""
    if paths.entry(path) is not None:
        if read_record(path) != record:
            raise SDKError(f'LLVM stage input record differs: {path}')
        return
    partial = path + '.partial-' + str(os.getpid())
    jsonio.write_json(partial, record, exclusive=True)
    try:
        os.link(partial, path)
    except FileExistsError:
        if read_record(path) != record:
            raise SDKError(f'LLVM stage input record differs: {path}') from None
    finally:
        os.unlink(partial)
