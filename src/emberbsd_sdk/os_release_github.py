# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD ember/release/github.rb; AI-assisted Python migration.
"""Immutable GitHub releases for OS kits. No network access on import."""

from pathlib import Path
import re
import os
import subprocess
import tempfile

from .errors import SDKError
from .inventory import sha256_file
from .jsonio import loads_json
from .process import sanitized_environment
from . import os_release as kit


class GitHub:
    def __init__(self, directory, profile, runner=None):
        self.directory = Path(directory).absolute()
        self.profile = profile
        self.manifest = kit.verify(self.directory, profile)
        self.runner = runner or self.run
        self.tag = self.manifest['release_tag']
        self.sha = self.manifest['source_commit']
        self.repo = 'github.com/' + kit.REPOSITORY

    @staticmethod
    def run(*args):
        environment = sanitized_environment()
        environment.update({k: os.environ[k] for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR') if k in os.environ})
        result = subprocess.run(['gh', *map(str, args)], capture_output=True, text=True,
                                env=environment, check=False)
        optional = args[0] == 'api' and re.match(r'^HTTP/\S+ 404\b', result.stdout)
        kit.check(result.returncode == 0 or optional, 'GitHub command failed: ' + result.stderr)
        return result.stdout

    def api(self, path, missing=False):
        raw = self.runner('api', '--include', '--hostname', 'github.com', '-H',
                          'X-GitHub-Api-Version: 2026-03-10', f'repos/{kit.REPOSITORY}/{path}')
        parts = re.split(r'\r?\n\r?\n', raw, maxsplit=1)
        match = re.match(r'^HTTP/\S+ (\d+)', parts[0])
        code = int(match[1]) if match else 0
        if missing and code == 404:
            return None
        kit.check(code == 200 and len(parts) == 2, f'GitHub API refused {path}: HTTP {code}')
        return loads_json(parts[1])

    def preflight(self):
        kit.check(self.api('immutable-releases')['enabled'] is True, 'Enable immutable releases before publication')
        kit.check(self.api(f'compare/{self.sha}...main')['status'] in ('ahead', 'identical'), 'The release source is not accepted in public main')
        refs = self.api('git/matching-refs/tags/' + self.tag)
        ref = next((r for r in refs if r['ref'] == 'refs/tags/' + self.tag), None)
        if ref:
            obj = ref['object']
            kit.check(obj['type'] == 'commit' and obj['sha'] == self.sha, 'Existing tag has another source; do not replace it')
        release = self.find_release()
        kit.check(not ref or release, 'Tag exists without the matching draft release')
        if release and not ref:
            kit.check(release['draft'] is True and release['target_commitish'] == self.sha, 'Pending release tag is not bound to the exact source')
        return release

    def find_release(self):
        release = self.api('releases/tags/' + self.tag, missing=True)
        if release:
            kit.check(release.get('tag_name', self.tag) == self.tag, 'Release tag changed during lookup')
            return release
        matches, page = [], 1
        while True:
            batch = self.api(f'releases?per_page=100&page={page}')
            kit.check(isinstance(batch, list), 'Invalid release list')
            matches.extend(r for r in batch if r['tag_name'] == self.tag)
            if len(batch) < 100:
                break
            page += 1
        kit.check(len(matches) <= 1, 'Multiple releases reserve the same tag')
        if not matches:
            return None
        release = self.api('releases/' + str(matches[0]['id']))
        kit.check(release['tag_name'] == self.tag, 'Release tag changed during lookup')
        return release

    @property
    def names(self):
        return [f['name'] for f in self.manifest['files']] + ['manifest.json', 'SHA256']

    @property
    def title(self):
        if self.profile == 'amd64':
            return f'EmberBSD {self.manifest["product_version"]} amd64 {self.sha[:12]} (no X11 VM candidate)'
        return f'EmberBSD {self.manifest["product_version"]} AArch64 {self.sha[:12]} development sysroot'

    @property
    def url(self):
        return f'https://github.com/{kit.REPOSITORY}/releases/tag/{self.tag}'

    def check_assets(self, release, complete):
        assets = release['assets']
        existing = [a['name'] for a in assets]
        kit.check(len(existing) == len(set(existing)) and set(existing).issubset(self.names), 'Draft has unexpected or duplicate assets')
        if complete:
            kit.check(set(existing) == set(self.names), 'Release assets are incomplete')
        for a in assets:
            path = self.directory / a['name']
            kit.check(a['state'] == 'uploaded' and a['size'] == path.stat().st_size and
                      a['digest'] == 'sha256:' + sha256_file(path), 'Remote asset differs: ' + a['name'] + '; do not clobber it')
        return [n for n in self.names if n not in existing]

    def check_local(self):
        kit.check(kit.verify(self.directory, self.profile) == self.manifest, 'Local kit changed since preflight')

    def check_draft(self, release):
        kit.check(release['draft'] is True, 'Release is already published; no asset writes are allowed')
        kit.check(release['prerelease'] is True and release['name'] == self.title and
                  release['body'] == (self.directory / 'release-notes.md').read_text(), 'Existing draft does not belong to this kit')

    def draft(self):
        self.check_local()
        release = self.preflight()
        if release is None:
            self.runner('release', 'create', self.tag, '--repo', self.repo, '--target', self.sha,
                        '--draft', '--prerelease', '--latest=false', '--title', self.title,
                        '--notes-file', str(self.directory / 'release-notes.md'))
            release = self.preflight()
            kit.check(release and release['draft'] is True, 'Draft creation is not confirmed')
        self.check_draft(release)
        missing = self.check_assets(release, complete=False)
        if missing:
            self.check_local()
            self.runner('release', 'upload', self.tag, '--repo', self.repo,
                        *(str(self.directory / n) for n in missing))
        self.verify_remote()
        return self.url

    def verify_remote(self):
        self.check_local()
        release = self.find_release()
        kit.check(release, 'Release is missing')
        self.check_assets(release, complete=True)
        with tempfile.TemporaryDirectory(prefix='ember-release-download-') as directory:
            patterns = [arg for name in self.names for arg in ('--pattern', name)]
            self.runner('release', 'download', self.tag, '--repo', self.repo, '--dir', directory, *patterns)
            kit.verify(directory, self.profile)
            for name in self.names:
                kit.check(sha256_file(Path(directory) / name) == sha256_file(self.directory / name), 'Downloaded asset differs: ' + name)
        if release['draft'] is False:
            kit.check(release['immutable'] is True, 'Published release is not immutable')
            self.verify_attestation()
        return release

    def verify_attestation(self):
        self.runner('release', 'verify', self.tag, '--repo', self.repo)
        for name in self.names:
            self.runner('release', 'verify-asset', self.tag, str(self.directory / name), '--repo', self.repo)

    def publish(self):
        self.check_local()
        release = self.preflight()
        kit.check(release, 'Create and verify the draft first')
        if release['draft'] is True:
            self.check_draft(release)
        release = self.verify_remote()
        if release['draft'] is True:
            self.check_draft(release)
            self.check_local()
            self.runner('release', 'edit', self.tag, '--repo', self.repo, '--draft=false', '--prerelease', '--latest=false')
            release = self.find_release()
        kit.check(release and release['draft'] is False and release['immutable'] is True, 'Immutable publication is not confirmed')
        self.verify_attestation()
        return self.url
