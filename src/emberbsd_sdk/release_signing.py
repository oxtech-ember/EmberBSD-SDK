# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""X.509 release signatures using the selected Ports OpenSSL and pkg_tools.

Cryptography belongs to those implementations. This module selects their inputs,
checks the release certificate profile and keeps unauthenticated output private.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from .errors import SDKError
from .host_prefix import load_host_prefix
from .paths import open_regular, verify_stable

MAX_ENVELOPE = 16 * 1024 * 1024
MAX_CONTENT = 15 * 1024 * 1024


def read_bytes(path, limit=MAX_ENVELOPE):
    handle, before = open_regular(path)
    with handle:
        value = handle.read(limit + 1)
    verify_stable(path, before)
    if len(value) > limit:
        raise SDKError(f'file exceeds the size limit: {path}')
    return value


def write_new(path, data, mode=0o644):
    """Publish one complete new file; never replace an existing release object."""
    path = Path(path).absolute()
    fd, name = tempfile.mkstemp(prefix='.release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(name)


@dataclass(frozen=True)
class SigningIdentity:
    certificate: Path
    key: Path


class ReleaseCrypto:
    def __init__(self, prefix):
        self.prefix = prefix

    @classmethod
    def from_prefix(cls, prefix, *, packages=False):
        roles = ['openssl']
        if packages:
            roles += ['pkg_admin']
        return cls(load_host_prefix(prefix, required_roles=roles))

    def _run(self, role, arguments, data=None):
        environment = {'PATH': self.prefix.path, 'LC_ALL': 'C', 'LANG': 'C',
                       'OPENSSL_CONF': os.devnull}
        if 'TMPDIR' in os.environ:
            environment['TMPDIR'] = os.environ['TMPDIR']
        try:
            result = subprocess.run([self.prefix.tool(role), *map(str, arguments)],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SDKError(f'{role} failed: {exc}') from exc
        if result.returncode:
            detail = result.stderr[-4096:].decode('utf-8', 'replace').strip()
            raise SDKError(f'{role} failed ({result.returncode}): {detail}')
        return result.stdout

    def run(self, arguments, *, data=None):
        return self._run('openssl', arguments, data)

    def run_package(self, role, arguments):
        if role not in ('pkg_admin', 'pkg_create'):
            raise SDKError(f'unsupported release package tool: {role}')
        return self._run(role, arguments)

    def _leaf_profile(self, certificate):
        pem = read_bytes(certificate, 128 * 1024)
        if pem.count(b'-----BEGIN CERTIFICATE-----') != 1:
            raise SDKError('release signer must contain exactly one leaf certificate')
        text = self.run(['x509', '-noout', '-ext',
            'basicConstraints,keyUsage,extendedKeyUsage'], data=pem).decode('ascii')
        extensions = {}
        current = None
        for line in text.splitlines():
            if 'X509v3 ' in line:
                current = line.strip().removeprefix('X509v3 ').split(':', 1)[0]
                extensions[current] = []
            elif line.strip() and current is not None:
                extensions[current].extend(part.strip() for part in line.split(','))
        expected = {'Basic Constraints': ['CA:FALSE'], 'Key Usage': ['Digital Signature'],
                    'Extended Key Usage': ['Code Signing', 'E-mail Protection']}
        if {key: set(value) for key, value in extensions.items()} != {
                key: set(value) for key, value in expected.items()}:
            raise SDKError('release certificate requires CA:FALSE, digitalSignature and '
                           'exactly codeSigning,emailProtection (pkg_tools profile)')

    def _trust(self, root, now=None):
        arguments = ['-CAfile', str(root), '-no-CAfile', '-no-CApath', '-no-CAstore',
                     '-purpose', 'smimesign', '-auth_level', '2', '-verify_depth', '1',
                     '-x509_strict', '-check_ss_sig']
        if now is not None:
            if type(now) is not int or now < 0:
                raise SDKError('invalid verification time')
            arguments += ['-attime', str(now)]
        return arguments

    def sign_cms(self, data, identity, root):
        if not isinstance(data, bytes) or not data or len(data) > MAX_CONTENT:
            raise SDKError('signed metadata must be nonempty bounded bytes')
        self._leaf_profile(identity.certificate)
        signed = self.run(['cms', '-sign', '-binary', '-nodetach', '-nosmimecap',
            '-md', 'sha256', '-outform', 'DER', '-signer', identity.certificate,
            '-inkey', identity.key], data=data)
        if self.verify_cms(signed, root) != data:
            raise SDKError('signed metadata did not round trip')
        return signed

    def verify_cms(self, data, root, *, now=None):
        if not isinstance(data, bytes) or not data or len(data) > MAX_ENVELOPE:
            raise SDKError('invalid CMS envelope size')
        with tempfile.TemporaryDirectory(prefix='ember-release-verify-') as directory:
            directory = Path(directory)
            anchor = directory / 'root.pem'
            anchor.write_bytes(read_bytes(root, 128 * 1024))
            output = directory / 'content'
            signer = directory / 'signer.pem'
            try:
                self.run(['cms', '-verify', '-binary', '-inform', 'DER',
                    *self._trust(anchor, now), '-out', output, '-signer', signer], data=data)
            except SDKError as exc:
                raise SDKError(f'CMS signature/certificate verification failed; check trust '
                               f'and the system clock: {exc}') from exc
            # OpenSSL writes signer certificates only after successful verification.
            self._leaf_profile(signer)
            return read_bytes(output, MAX_CONTENT)

    def create_root(self, directory):
        directory = Path(directory).absolute()
        directory.mkdir(mode=0o700)
        identity = SigningIdentity(directory / 'certificate.pem', directory / 'private-key.pem')
        write_new(identity.key, b'', mode=0o600)
        self.run(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
                  '-out', identity.key])
        self.run(['req', '-new', '-x509', '-sha256', '-days', '3650', '-key', identity.key,
            '-out', identity.certificate, '-subj', '/CN=EmberBSD Release Root',
            '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
            '-addext', 'subjectKeyIdentifier=hash'])
        return identity

    def issue_signer(self, root, directory, *, days=365):
        if type(days) is not int or not 1 <= days <= 365:
            raise SDKError('release certificate lifetime must be 1..365 days')
        directory = Path(directory).absolute()
        directory.mkdir(mode=0o700)
        identity = SigningIdentity(directory / 'certificate.pem', directory / 'private-key.pem')
        write_new(identity.key, b'', mode=0o600)
        self.run(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
                  '-out', identity.key])
        with tempfile.TemporaryDirectory(prefix='.request-', dir=directory) as temporary:
            request = Path(temporary) / 'request.pem'
            extensions = Path(temporary) / 'extensions.cnf'
            extensions.write_text('basicConstraints=critical,CA:FALSE\n'
                'keyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning,emailProtection\n'
                'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n', encoding='ascii')
            self.run(['req', '-new', '-sha256', '-key', identity.key, '-out', request,
                      '-subj', '/CN=EmberBSD Release Signer'])
            self.run(['x509', '-req', '-sha256', '-in', request, '-CA', root.certificate,
                '-CAkey', root.key, '-set_serial', '0x' + secrets.token_hex(19),
                '-days', str(days), '-extfile', extensions, '-out', identity.certificate])
        self._leaf_profile(identity.certificate)
        self.run(['verify', *self._trust(root.certificate), identity.certificate])
        return identity

    def verify_package(self, package, root):
        # pkg_admin versions with permissive missing-file handling must not accept it.
        handle, before = open_regular(package)
        handle.close()
        with tempfile.TemporaryDirectory(prefix='ember-package-verify-') as directory:
            anchor = Path(directory) / 'root.pem'
            anchor.write_bytes(read_bytes(root, 128 * 1024))
            config = Path(directory) / 'pkg_install.conf'
            config.write_text(f'CERTIFICATE_ANCHOR_PKGS={anchor}\nVERIFIED_INSTALLATION=always\n'
                              f'GPG_KEYRING_VERIFY={os.devnull}\n', encoding='utf-8')
            self.run_package('pkg_admin', ['-C', config, 'check-signature', Path(package).absolute()])
        verify_stable(package, before)

    def sign_package(self, source, output, identity, root):
        self._leaf_profile(identity.certificate)
        handle, before = open_regular(source)
        handle.close()
        output = Path(output).absolute()
        with tempfile.TemporaryDirectory(prefix='.package-sign-', dir=output.parent) as directory:
            signed = Path(directory) / 'signed.tgz'
            config = Path(directory) / 'pkg_install.conf'
            config.write_bytes(b'')
            self.run_package('pkg_admin', ['-C', config, 'x509-sign-package', Path(source).absolute(), signed,
                                          identity.key, identity.certificate])
            verify_stable(source, before)
            self.verify_package(signed, root)
            # Link the completed archive without loading large packages into memory.
            with signed.open('rb') as handle:
                os.fsync(handle.fileno())
            os.link(signed, output)
            parent_fd = os.open(output.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
