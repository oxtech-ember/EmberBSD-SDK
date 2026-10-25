# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""One Ports-owned make binding and a finite bootstrap routing probe."""

import hashlib
import os
import re
import shlex

from .inventory import sha256_file, snapshot_tree
from .native_toolset import (_absolute, _bytes, _envelope, _json, _require, _sha, _source)
from .paths import canonical_existing, inside

BOOTSTRAP_MAKE_API = 1
MAKE_PROBE_FORMAT = 'emberbsd-bootstrap-make-probe-plan'
_KIND = 'ports-bootstrap-make-v1'
_TEMPLATE_RELATIVE = 'profiles/common-build-tools/bootstrap/sys.mk.in'
_TEMPLATE_SHA256 = 'c8c05581827fa28185b061b1f8228a2c5554b06910aeee628c07d4d94c782b99'
MAKECONF_SOURCE = (b'# SPDX-License-Identifier: BSD-2-Clause\n'
                   b'# This file is only for the finite bootstrap make pilot.\n'
                   b'EMBERBSD_BOOTSTRAP_MAKECONF=finite-bootstrap-make-v1\n')


def _safe_path(path):
    path = _absolute(path)
    _require(re.fullmatch(r'[A-Za-z0-9_./+@%=-]+', path),
             'fixed make binding requires an ASCII path without make metacharacters')
    return path


def bind_make_commands(descriptor, commands, *, inspect_inputs):
    """Check exact source and copied mk bytes. Do not execute or copy tools."""
    value = descriptor.get('make_binding')
    if value is None:
        return commands
    _require(isinstance(value, dict) and set(value) ==
             {'kind', 'ports_source', 'template_relative', 'template', 'original_inventory',
              'mk_root', 'inventory', 'shell', 'makeconf', 'source_correlation', 'mode_policy'},
             'unsupported bootstrap make binding fields')
    _require(value['kind'] == _KIND and {'make', 'gmake', 'sh'} <= set(commands),
             'bootstrap make binding needs selected make, gmake, and sh roles')
    _source(value['ports_source'])
    _require(value['ports_source'] == descriptor['sources']['ports'] and
             value['template_relative'] == _TEMPLATE_RELATIVE,
             'bootstrap make binding source mismatch')
    for name in ('template', 'makeconf', 'source_correlation'):
        ref = value[name]
        _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'},
                 'bootstrap make binding needs exact file references')
        _absolute(ref['path'])
        _sha(ref['sha256'])
    _require(value['template']['sha256'] == _TEMPLATE_SHA256,
             'unsupported bootstrap make entry template')
    root = _absolute(descriptor['bundle_root'])
    mk = _safe_path(value['mk_root'])
    shell = _safe_path(value['shell'])
    conf = _safe_path(value['makeconf']['path'])
    _require(shell == commands['sh'][0] and commands['sh'] == (shell,) and
             mk != root and os.path.commonpath((mk, root)) == root,
             'bootstrap make binding changed the shell or escaped its bundle')
    for role in ('make', 'gmake'):
        _safe_path(commands[role][0])
    original, current = _json(value['original_inventory']), _json(value['inventory'])
    from .bootstrap_seed import _relative
    _require(len(original) == 21 and 'sys.mk' in original and 'sys-upstream.mk' not in original and
             all(_relative(name) == name and row.get('kind') == 'file'
                 for name, row in original.items()), 'the complete accepted 21-file mk tree is required')
    _require(value['mode_policy'] == 'read-only', 'unsupported bootstrap mk mode policy')
    expected = {name: dict(row, mode=row['mode'] & 0o555) for name, row in original.items()}
    expected['sys-upstream.mk'] = expected.pop('sys.mk')
    entry = current.get('sys.mk', {})
    _require(entry.get('kind') == 'file' and entry.get('mode') == expected['sys-upstream.mk']['mode'],
             'bootstrap make entry is not a read-only regular file')
    _sha(entry.get('sha256'))
    expected['sys.mk'] = entry
    _require(current == expected, 'bootstrap make tree does not preserve the complete original roster')
    if inspect_inputs:
        template = _bytes(value['template'])
        instantiated = template.replace(b'@QUALIFIED_SH@', shell.encode('ascii'))
        _require(entry['sha256'] == hashlib.sha256(instantiated).hexdigest(),
                 'bootstrap make entry differs from the exact Ports instantiation')
        _require(canonical_existing(mk) == mk and inside(mk, root) and snapshot_tree(mk) == current,
                 'bootstrap make tree changed or escaped its bundle')
        _bytes(value['makeconf'])
        _bytes(value['source_correlation'])
    result = dict(commands)
    result['make'] = (commands['make'][0], '-m', mk, 'SHELL=' + shell, 'MAKECONF=' + conf)
    result['gmake'] = (commands['gmake'][0], 'SHELL=' + shell, 'MAKECONF=' + conf)
    return result


def make_probe_sources(descriptor, commands):
    binding = descriptor['make_binding']
    shell, mk, conf = binding['shell'], binding['mk_root'], binding['makeconf']['path']
    common_bsd = (
        f'.if "${{.SHELL}}" != "{shell}" || "${{SHELL}}" != "{shell}"\n'
        '.error bootstrap shell selection changed\n.endif\n'
        f'.if "${{.SYSPATH}}" != "{mk}"\n.error bootstrap system path changed\n.endif\n'
        '.include <bsd.own.mk>\n'
        f'.if "${{MAKECONF}}" != "{conf}" || "${{EMBERBSD_BOOTSTRAP_MAKECONF:U}}" != "finite-bootstrap-make-v1"\n'
        '.error bootstrap MAKECONF was not loaded\n.endif\n'
        '.if empty(.SUFFIXES:M.c) || empty(.SUFFIXES:M.o)\n.error upstream suffixes were lost\n.endif\n'
        "ASSIGN != print -r -- \"$${KSH_VERSION-}\"\n"
        "MOD_COMMAND = print -r -- \"$${KSH_VERSION-}\"\n"
        'MODIFIER := ${MOD_COMMAND:sh}\n'
        '.if empty(ASSIGN:M*MIRBSD*) || empty(MODIFIER:M*MIRBSD*)\n.error parse-time shell changed\n.endif\n')
    bsd = (common_bsd + '.PHONY: all fail\nall: fixture.result\n'
           "\t@print -r -- bmake-assign-ok; print -r -- bmake-modifier-ok; print -r -- upstream-suffixes-ok\n"
           f"\t@print -r -- 'bmake-shell ${{.SHELL}}'\n"
           f'\t+{shlex.join(commands["make"])} -j1 -f BRecursive.mk child\n'
           "\t@print -r -- bmake-positive-ok\n"
           '.SUFFIXES: .probe .result\n.probe.result:\n'
           "\t@case \"$${KSH_VERSION-}\" in *MIRBSD*) ;; *) exit 42;; esac\n"
           "\t@print -r -- implicit-ok > '${.TARGET}'\n"
           "fail:\n\t@print -r -- expected-bmake-failure; exit 7\n")
    bsd_recursive = (common_bsd + '.PHONY: child\nchild:\n'
                     "\t@case \"$${KSH_VERSION-}\" in *MIRBSD*) ;; *) exit 42;; esac\n"
                     "\t@print -r -- bmake-recursive-ok\n")
    common_gnu = (f'ifneq ($(SHELL),{shell})\n$(error bootstrap GNU shell changed)\nendif\n'
                  f'ifneq ($(MAKECONF),{conf})\n$(error bootstrap GNU MAKECONF changed)\nendif\n'
                  'include $(MAKECONF)\n'
                  'ifneq ($(EMBERBSD_BOOTSTRAP_MAKECONF),finite-bootstrap-make-v1)\n'
                  '$(error bootstrap GNU MAKECONF was not loaded)\nendif\n')
    gnu = (common_gnu + '.PHONY: all fail\nall:\n'
           "\t@case \"$${KSH_VERSION-}\" in *MIRBSD*) ;; *) exit 42;; esac\n"
           f"\t@print -r -- 'gmake-shell $(SHELL)'\n"
           f'\t+{shlex.join(commands["gmake"])} --no-print-directory -j1 -f GRecursive.mk child\n'
           "\t@print -r -- gmake-positive-ok\n"
           "fail:\n\t@print -r -- expected-gmake-failure; exit 7\n")
    gnu_recursive = (common_gnu + '.PHONY: child\nchild:\n'
                     "\t@case \"$${KSH_VERSION-}\" in *MIRBSD*) ;; *) exit 42;; esac\n"
                     "\t@print -r -- gmake-recursive-ok\n")
    return {name: text.encode('ascii') for name, text in
            [('BMakefile', bsd), ('BRecursive.mk', bsd_recursive),
             ('GMakefile', gnu), ('GRecursive.mk', gnu_recursive), ('fixture.probe', 'finite-input\n')]}


def make_probe_steps(descriptor, commands, flags, work_root):
    from .bootstrap_seed import _environment
    work = _absolute(work_root)
    env = _environment({role: words[0] for role, words in commands.items()}, commands, flags)
    env.update(LC_ALL='C', TZ='UTC', TMPDIR=work, MAKECONF=descriptor['make_binding']['makeconf']['path'])
    positive = 'case "${KSH_VERSION-}" in *MIRBSD*) ;; *) exit 42;; esac; print -r -- shell-positive-ok'
    rows = [('shell-positive', 'shell', [*commands['sh'], '-c', positive], 0),
            ('bmake-positive', 'make', [*commands['make'], '-j1', '-f', 'BMakefile', 'all'], 0),
            ('gmake-positive', 'make', [*commands['gmake'], '--no-print-directory', '-j1', '-f', 'GMakefile', 'all'], 0),
            ('shell-failure', 'shell', [*commands['sh'], '-c', 'print -r -- expected-shell-failure; exit 7'], 7),
            ('bmake-failure', 'make', [*commands['make'], '-j1', '-f', 'BMakefile', 'fail'], 2),
            ('gmake-failure', 'make', [*commands['gmake'], '--no-print-directory', '-j1', '-f', 'GMakefile', 'fail'], 2)]
    return [dict(id=name, phase=phase, argv=argv, cwd=work, env=dict(env), expected_exit=status)
            for name, phase, argv, status in rows]


def validate_make_probe_plan(plan, descriptor, commands, flags):
    _envelope(plan, MAKE_PROBE_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'purpose', 'seed_descriptor', 'host',
                          'sources', 'work_root', 'required_roles', 'resources', 'commands'} and
             plan['purpose'] == 'make-and-shell-routing-probe' and
             plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'],
             'unsupported finite bootstrap make probe plan')
    _require(descriptor.get('make_binding', {}).get('kind') == _KIND,
             'finite make probe requires the recognized Ports binding')
    for source in plan['sources'].values():
        _source(source)
    roles = plan['required_roles']
    _require(isinstance(roles, list) and len(roles) == len(set(roles)) and
             {'make', 'gmake', 'sh'} <= set(roles) <= set(commands), 'finite make probe roles are incomplete')
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'])
    _require(_bytes(descriptor['make_binding']['makeconf']) == MAKECONF_SOURCE,
             'finite make probe requires its exact fixture MAKECONF')
    _require(plan['commands'] == make_probe_steps(descriptor, commands, flags, plan['work_root']),
             'make probe commands differ from the source-owned finite route')
    return plan


def verify_make_step(step, log, work, descriptor):
    data = _bytes(dict(path=log, sha256=sha256_file(log)))
    required = {'shell-positive': ['shell-positive-ok'],
                'bmake-positive': ['bmake-positive-ok', 'bmake-recursive-ok', 'bmake-assign-ok',
                                   'bmake-modifier-ok', 'upstream-suffixes-ok',
                                   'bmake-shell ' + descriptor['make_binding']['shell']],
                'gmake-positive': ['gmake-positive-ok', 'gmake-recursive-ok',
                                   'gmake-shell ' + descriptor['make_binding']['shell']],
                'shell-failure': ['expected-shell-failure'],
                'bmake-failure': ['expected-bmake-failure'],
                'gmake-failure': ['expected-gmake-failure']}[step['id']]
    lines = data.decode('utf-8', 'strict').splitlines()
    _require(all(line in lines for line in required), 'make probe did not produce its required observations')
    if step['id'] == 'bmake-positive':
        path = os.path.join(work, 'fixture.result')
        _require(_bytes(dict(path=path, sha256=sha256_file(path))) == b'implicit-ok\n',
                 'finite implicit suffix rule did not produce its expected output')
