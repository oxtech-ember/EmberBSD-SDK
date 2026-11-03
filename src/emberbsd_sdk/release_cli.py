# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Produce and verify the single signed release and channel format."""

import argparse
import hashlib
from pathlib import Path
import sys

from .errors import SDKError
from .jsonio import json_bytes, loads_json
from .paths import regular_file, verify_stable
from .release_catalog import (commit_channel, file_reference, local_artifact,
                              parse_channel, parse_release, verify_payloads)
from .release_signing import ReleaseCrypto, SigningIdentity, read_bytes, write_new


def _check_packages(crypto, release, directory, trust):
    for artifact in release['artifacts']:
        if artifact['kind'] in ('host-package', 'target-package'):
            crypto.verify_package(local_artifact(directory, artifact['path']), trust)


def verify_release(crypto, envelope, directory, trust):
    release = parse_release(crypto.verify_cms(envelope, trust))
    verify_payloads(release, directory)
    _check_packages(crypto, release, directory, trust)
    return release


def seal_release(crypto, draft, directory, identity, trust, output):
    """Hash complete payloads before signing; the draft is a current-format inventory."""
    value = loads_json(draft)
    if not isinstance(value, dict) or not isinstance(value.get('artifacts'), list):
        raise SDKError('release inventory requires an artifacts array')
    for artifact in value['artifacts']:
        if not isinstance(artifact, dict):
            raise SDKError('release inventory artifact must be an object')
        artifact['size'] = 0
        artifact['sha256'] = '0' * 64
    parse_release(json_bytes(value))
    inputs = []
    for artifact in value['artifacts']:
        path = local_artifact(directory, artifact['path'])
        inputs.append((artifact['path'], path, regular_file(path)))
        artifact.update(file_reference(path))
    content = json_bytes(value)
    release = parse_release(content)
    _check_packages(crypto, release, directory, trust)
    signed = crypto.sign_cms(content, identity, trust)
    # Hash once; detect changes across signing without rereading every payload.
    for relative, path, before in inputs:
        local_artifact(directory, relative)
        verify_stable(path, before)
    write_new(output, signed)
    return release


def _referenced_release(crypto, channel, directory, trust):
    reference = channel['release']
    path = local_artifact(directory, reference['path'])
    envelope = read_bytes(path)
    if len(envelope) != reference['size'] or hashlib.sha256(envelope).hexdigest() != reference['sha256']:
        raise SDKError('channel release envelope size or SHA256 differs')
    release = parse_release(crypto.verify_cms(envelope, trust))
    if release['release_id'] != reference['id']:
        raise SDKError('channel and release identities differ')
    return release


def seal_channel(crypto, draft, directory, identity, trust, output):
    channel = loads_json(draft)
    if not isinstance(channel, dict) or not isinstance(channel.get('release'), dict):
        raise SDKError('channel inventory requires a release reference')
    channel['release'].update(size=1, sha256='0' * 64)
    channel = parse_channel(json_bytes(channel), expected_name=channel.get('channel'))
    channel['release'].update(file_reference(local_artifact(directory, channel['release']['path'])))
    _referenced_release(crypto, channel, directory, trust)
    content = json_bytes(channel)
    parse_channel(content, expected_name=channel['channel'])
    write_new(output, crypto.sign_cms(content, identity, trust))
    return channel


def verify_channel(crypto, envelope, directory, trust, name, state):
    content = crypto.verify_cms(envelope, trust)
    channel = parse_channel(content, expected_name=name)
    release = _referenced_release(crypto, channel, directory, trust)
    # Accept catalog metadata without requiring every release payload locally.
    # Installation must verify the selected payloads before changing a prefix.
    commit_channel(content, state, expected_name=name)
    return release


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--host-prefix', required=True, help='native Ports prefix (OpenSSL and pkg_tools)')
    commands = result.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-root', help='create a new private root directory')
    create.add_argument('directory', type=Path)
    issue = commands.add_parser('issue-signer', help='issue or rotate a release leaf under the same root')
    issue.add_argument('--root-directory', type=Path, required=True)
    issue.add_argument('directory', type=Path)
    issue.add_argument('--days', type=int, default=365)
    for command in ('sign-package', 'verify-package', 'seal-release', 'verify-release',
                    'seal-channel', 'verify-channel'):
        sub = commands.add_parser(command)
        sub.add_argument('--trust-root', type=Path, required=True,
                         help='previously provisioned root PEM; never trust a root from the channel')
        if command.startswith(('sign-', 'seal-')):
            sub.add_argument('--signer-directory', type=Path, required=True)
            sub.add_argument('--output', type=Path, required=True, help='new output; existing paths are refused')
        if command.endswith(('release', 'channel')):
            sub.add_argument('--directory', type=Path, required=True,
                             help='release payload directory, or repository root for channels')
        if command == 'verify-channel':
            sub.add_argument('--channel', required=True)
            sub.add_argument('--state', type=Path, required=True,
                             help='persistent local state file in an existing private directory')
        sub.add_argument('input', type=Path)
    return result


def _identity(directory):
    return SigningIdentity(directory / 'certificate.pem', directory / 'private-key.pem')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        crypto = ReleaseCrypto.from_prefix(args.host_prefix,
            packages=args.command not in ('create-root', 'issue-signer'))
        if args.command == 'create-root':
            identity = crypto.create_root(args.directory)
            print(f'Created root certificate: {identity.certificate}')
        elif args.command == 'issue-signer':
            identity = crypto.issue_signer(_identity(args.root_directory), args.directory, days=args.days)
            print(f'Created release certificate: {identity.certificate}')
        elif args.command == 'sign-package':
            crypto.sign_package(args.input, args.output, _identity(args.signer_directory), args.trust_root)
            print(f'Signed package: {args.output}')
        elif args.command == 'verify-package':
            crypto.verify_package(args.input, args.trust_root)
            print(f'Package signature verified: {args.input}')
        else:
            content = read_bytes(args.input)
            if args.command in ('seal-release', 'seal-channel'):
                operation = seal_release if args.command == 'seal-release' else seal_channel
                value = operation(crypto, content, args.directory, _identity(args.signer_directory),
                                  args.trust_root, args.output)
            elif args.command == 'verify-release':
                value = verify_release(crypto, content, args.directory, args.trust_root)
            else:
                value = verify_channel(crypto, content, args.directory, args.trust_root,
                                       args.channel, args.state)
            sys.stdout.buffer.write(json_bytes(value))
        return 0
    except (SDKError, OSError) as exc:
        print(f'release: {exc}', file=sys.stderr)
        return 1
