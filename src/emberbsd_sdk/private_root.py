# SPDX-License-Identifier: BSD-2-Clause
"""Explicit private EmberBSD root personalization. Never publish or install."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys

from .errors import SDKError
from .host_prefix import load_host_prefix
from .host_services import build_path
from .inventory import sha256_file
from .process import run_logged, sanitized_environment

def require(condition, reason):
    if not condition:
        raise SDKError(reason)

def regular(path):
    require(stat.S_ISREG(path.lstat().st_mode), 'private input must be a regular file')
    return path

def directory(path):
    require(stat.S_ISDIR(path.lstat().st_mode), 'private input must be a real directory')
    return path

def executable(path):
    regular(path.resolve(strict=True))
    require(path.is_absolute() and os.access(path, os.X_OK), 'explicit executable is unavailable')
    return str(path)

def escape(value):
    # mtree uses byte-oriented octal escapes, including for UTF-8 names.
    return ''.join(chr(c) if 33 <= c <= 126 and c not in (35, 92)
                   else '\\%03o' % c for c in str(value).encode('utf-8'))

def public_key(value):
    words = value.strip().split()
    require(len(words) >= 2 and words[0] == 'ssh-ed25519', 'expected one Ed25519 public key')
    try:
        wire = base64.b64decode(words[1], validate=True)
    except ValueError:
        raise SDKError('invalid Ed25519 public key') from None
    require(len(wire) == 51 and wire[:19] == b'\0\0\0\x0bssh-ed25519\0\0\0\x20',
            'invalid Ed25519 public key')
    return 'ssh-ed25519 ' + words[1], wire

def copy_layer(source, target, entries, overlay=False, preserve_ownership=False):
    """Copy without following links; retain target ownership in mtree."""
    links = {}
    def visit(src, dst, relative):
        info = src.lstat()
        mode = info.st_mode
        require(not any(ord(c) < 32 or ord(c) == 127 for c in relative), 'invalid root entry name')
        fields = dict(type='', uid=info.st_uid if preserve_ownership else 0,
                      gid=info.st_gid if preserve_ownership else 0,
                      mode=format(stat.S_IMODE(mode), 'o'))
        exists = os.path.lexists(dst)
        if stat.S_ISDIR(mode):
            require(not exists or stat.S_ISDIR(dst.lstat().st_mode), 'overlay directory collides with a non-directory')
            dst.mkdir(mode=0o700, exist_ok=True)
            fields['type'] = 'dir'
            if not (overlay and relative == '.'):
                entries[relative] = fields
            for child in sorted(src.iterdir()):
                name = child.name if relative == '.' else relative + '/' + child.name
                visit(child, dst / child.name, name)
            # Parent output is private even when target directory modes are open.
            dst.chmod(stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            return
        require(not exists or not stat.S_ISDIR(dst.lstat().st_mode), 'overlay file collides with a directory')
        if exists:
            dst.unlink()
        if stat.S_ISLNK(mode):
            link = os.readlink(src)
            require('\0' not in link and '\n' not in link, 'invalid target link')
            # Absolute links are interpreted by the future target, never followed here.
            fields.update(type='link', link=link)
            os.symlink(link, dst)
        elif stat.S_ISREG(mode):
            fields['type'] = 'file'
            identity = (info.st_dev, info.st_ino)
            if info.st_nlink > 1 and identity in links:
                os.link(links[identity], dst)
            else:
                shutil.copyfile(src, dst, follow_symlinks=False)
                dst.chmod(stat.S_IMODE(mode))
                links[identity] = dst
        elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            # makefs creates target nodes from metadata; no host mknod.
            fields.update(type='char' if stat.S_ISCHR(mode) else 'block',
                          device='netbsd,%d,%d' % (os.major(info.st_rdev), os.minor(info.st_rdev)))
        else:
            raise SDKError('runtime tree contains an unsupported special file')
        entries[relative] = fields
    visit(source, target, '.')

def target_file(tree, entries, name, content, mode=0o600):
    parts = PurePosixPath(name).parts
    parent = tree
    for index, part in enumerate(parts[:-1]):
        parent /= part
        relative = '/'.join(parts[:index + 1])
        if os.path.lexists(parent):
            require(stat.S_ISDIR(parent.lstat().st_mode), 'private file parent is not a real directory')
        else:
            parent.mkdir(mode=0o700)
            entries[relative] = dict(type='dir', uid=0, gid=0, mode='755')
    path = tree / name
    if os.path.lexists(path):
        require(stat.S_ISREG(path.lstat().st_mode), 'private output collides with a non-file')
        path.unlink()  # Do not modify another inherited hardlink.
    with path.open('xb') as stream:
        stream.write(content)
    path.chmod(mode)
    entries[name] = dict(type='file', uid=0, gid=0, mode=format(mode, 'o'))
    return path

def identity_parents(tree, entries):
    # Check target metadata before any private identity is written.
    parents = {'.'}
    for name in ('etc/ssh', 'root/.ssh', 'etc/ember', 'var/db'):
        parts = PurePosixPath(name).parts
        parents.update('/'.join(parts[:index]) for index in range(1, len(parts) + 1))
    for name in sorted(parents, key=lambda item: (item.count('/'), item)):
        path = tree if name == '.' else tree / name
        if not os.path.lexists(path):
            continue  # target_file creates missing directories as root:root 0755.
        require(stat.S_ISDIR(path.lstat().st_mode), 'private file parent is not a real directory')
        fields = entries[name]
        require(fields['uid'] == 0 and int(fields['mode'], 8) & 0o022 == 0,
                'private file parent must be root-owned and not writable by other users')

def device_entries(text):
    result = []
    for line in text.splitlines():
        if not line or line.startswith('#') or line.startswith('. type=dir'):
            continue
        fields = line.split()
        require(fields and fields[0].startswith('./'), 'invalid MAKEDEV entry')
        path = PurePosixPath(fields[0][2:])
        require(path.parts and all(p not in ('..', '.') for p in path.parts) and
                not path.is_absolute() and '\\' not in fields[0],
                'MAKEDEV entry escapes dev')
        require(all('=' in field for field in fields[1:]), 'invalid MAKEDEV metadata')
        attributes = dict(field.split('=', 1) for field in fields[1:])
        require(set(attributes) <= {'type', 'uid', 'gid', 'mode', 'device', 'link'} and
                attributes.get('type') in ('dir', 'char', 'block', 'link'),
                'unexpected MAKEDEV metadata')
        result.append(('./dev/' + path.as_posix(), attributes))
    require(bool(result), 'MAKEDEV emitted no devices')
    return result

def personalize(options, *, native=None):
    root = directory(Path(options.root_tree)).resolve()
    overlay = directory(Path(options.overlay)).resolve()
    output = Path(options.output)
    require(output.is_absolute(), 'private output must be absolute')
    output = directory(output.parent).resolve() / output.name
    require(not os.path.lexists(output), 'private output already exists')
    require(not output.is_relative_to(root) and not output.is_relative_to(overlay) and
            not root.is_relative_to(output) and not overlay.is_relative_to(output),
            'private output overlaps an input')
    require(isinstance(options.device_id, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', options.device_id),
            'invalid caller device binding')
    require(type(options.size) is int and options.size >= 1048576 and options.size % 512 == 0,
            'FFS size must be sector-aligned bytes')
    require(options.byte_order in ('little', 'big'), 'explicit target byte order is required')
    for value in (options.machine, options.machine_arch, *options.device):
        require(re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None, 'invalid MAKEDEV selection')
    require(bool(options.device), 'select MAKEDEV devices explicitly')
    require(all(not value.startswith('-') for value in options.device), 'MAKEDEV device must not be an option')
    authorized, _ = public_key(regular(Path(options.authorized_key)).read_text())
    version = regular(Path(options.kernel_version_file)).read_text().rstrip('\n')
    require(version and len(version.encode()) <= 8192 and '\0' not in version, 'invalid producer kernel identity')
    boot_sha = sha256_file(regular(Path(options.boot_image)))
    makedev = regular(Path(options.makedev)).resolve()
    os_source = directory(Path(options.os_source)).resolve()
    keygen = executable(Path(options.ssh_keygen))
    makefs = executable(Path(options.tools) / 'bin/nbmakefs')
    native = native or load_host_prefix(options.host_prefix, required_roles=('sh', 'awk', 'sed'))
    output.mkdir(mode=0o700)
    # Restrict every generated log/file from its creation, including keygen outputs.
    old_umask = os.umask(0o077)
    try:
        tree = output / 'root'
        entries = {}
        preserve = getattr(options, 'preserve_target_ownership', False)
        copy_layer(root, tree, entries, preserve_ownership=preserve)
        copy_layer(overlay, tree, entries, overlay=True, preserve_ownership=preserve)
        identity_parents(tree, entries)
        ssh_dir = tree / 'etc/ssh'
        target_file(tree, entries, 'etc/ssh/.personalizing', b'')
        (ssh_dir / '.personalizing').unlink(); del entries['etc/ssh/.personalizing']
        for old in ssh_dir.glob('ssh_host_*'):
            require(not stat.S_ISDIR(old.lstat().st_mode), 'inherited host-key path is a directory')
            old.unlink(); entries.pop(old.relative_to(tree).as_posix(), None)
        env = sanitized_environment()
        env.update(PATH=build_path(native, str(output)), HOME=str(output), LC_ALL='C')
        key = ssh_dir / 'ssh_host_ed25519_key'
        run_logged([keygen, '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(key)],
                   output / 'keygen.log', env=env, timeout_seconds=60)
        regular(key)
        host_public, wire = public_key(regular(Path(str(key) + '.pub')).read_text())
        for path in (key, Path(str(key) + '.pub')):
            path.chmod(0o600)
            entries[path.relative_to(tree).as_posix()] = dict(type='file', uid=0, gid=0, mode='600')
        nonce = os.urandom(24).hex()
        target_file(tree, entries, 'root/.ssh/authorized_keys', (authorized + '\n').encode())
        entries['root/.ssh'].update(uid=0, gid=0, mode='700')
        (tree / 'root/.ssh').chmod(0o700)
        target_file(tree, entries, 'etc/ember/firstboot-pending', (nonce + '\n').encode())
        seed = struct.pack('<I' if options.byte_order == 'little' else '>I', 256) + os.urandom(512)
        target_file(tree, entries, 'var/db/entropy-file', seed + hashlib.sha1(seed).digest())
        require(stat.S_ISDIR((tree / 'dev').lstat().st_mode), 'target dev directory is missing')
        env.update(MACHINE=options.machine, MACHINE_ARCH=options.machine_arch, NETBSDSRCDIR=str(os_source))
        run_logged([native.tool('sh'), str(makedev), '-s', *options.device],
                   output / 'devices.mtree', cwd=str(output), env=env, timeout_seconds=60)
        devices = device_entries((output / 'devices.mtree').read_text())
        # MAKEDEV is authoritative for selected device entries.
        for name, fields in devices:
            entries[name[2:]] = fields
        spec = output / 'root.mtree'
        with spec.open('x') as stream:
            for name in sorted(entries, key=lambda x: (x.count('/'), x)):
                path = '.' if name == '.' else './' + name
                stream.write(escape(path) + ' ' + ' '.join(k + '=' + escape(v) for k, v in entries[name].items()) + '\n')
        image = output / 'root.ffs'
        image.touch(mode=0o600, exist_ok=False)
        run_logged([makefs, '-t', 'ffs', '-B', options.byte_order, '-o', 'version=2',
                    '-s', str(options.size), '-F', str(spec), '-N', str(tree / 'etc'),
                    str(image), str(tree)], output / 'makefs.log', env=env, timeout_seconds=600)
        require(image.stat().st_size == options.size, 'makefs image size does not match the requested size')
        metadata = dict(schema='emberbsd.private-root.v1', device_id=options.device_id,
            nonce=nonce, host_key=host_public, host_key_sha256=hashlib.sha256(wire).hexdigest(),
            kernel_version=version, kernel_version_sha256=hashlib.sha256(version.encode()).hexdigest(),
            boot_sha256=boot_sha, root_sha256=sha256_file(image),
            root_bytes=image.stat().st_size, ownership='source-target-metadata' if preserve else 'root',
            installation_ready=False)
        (output / 'private.json').write_text(json.dumps(metadata, indent=2) + '\n')
        return dict(status='private_root_prepared', installation_ready=False)
    finally:
        os.umask(old_umask)

def main(argv):
    parser = argparse.ArgumentParser(description='Prepare one private root from a ready runtime tree; no installation or publication.')
    for name in ('root-tree', 'overlay', 'authorized-key', 'boot-image', 'kernel-version-file',
                 'host-prefix', 'ssh-keygen', 'tools', 'makedev', 'os-source', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('device-id', 'machine', 'machine-arch'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--device', action='append', required=True)
    parser.add_argument('--byte-order', choices=('little', 'big'), required=True)
    parser.add_argument('--size', type=int, required=True, help='FFS size in bytes')
    parser.add_argument('--preserve-target-ownership', action='store_true',
                        help='explicitly trust input UID/GID as target metadata; otherwise use UID/GID 0')
    options = parser.parse_args(argv)
    try:
        result = personalize(options)
        print(json.dumps(result))
        return 0
    except (SDKError, OSError, ValueError, UnicodeError):
        # Paths, device identity, native output and credentials remain private.
        print('Private root preparation failed; inspect the private output and inputs.', file=sys.stderr)
        return 1
