# Prepare libedit for OS builds

Ports owns the source pin and all patches in `pkgsrc/devel/editline`.
The SDK prepares that recipe for the native OS make rules. It does not vendor
another source copy or define another version. Packages use the same recipe.

Download the archive from the recipe's literal `MASTER_SITES` URL. Keep its
original bytes. Use a clean, fully materialized Ports checkout. Select a new
output directory with an existing parent:

```sh
python3 scripts/prepare-libedit.py \
  --ports /absolute/EmberBSD-Ports \
  --archive /absolute/libedit-20260512-3.1.tar.gz \
  --output /absolute/prepared-libedit
python3 scripts/prepare-libedit.py \
  --ports /absolute/EmberBSD-Ports --validate /absolute/prepared-libedit
```

Preparation requires Git and `patch` on PATH. Python uses only its standard
library. The command verifies archive BLAKE2s, SHA512 and size against distinfo.
It verifies every patch SHA1 with the pkgsrc RCS-line filtering rule. Archive
extraction accepts only regular files and directories below one source root.
It applies every patch with `patch -p0 -F0 --posix --no-backup-if-mismatch`, without reverse or
interactive fallback. The explicit mismatch policy does not create backup files.
Offsets remain valid. Reject files and unexpected backup files still fail
validation. A failure does not publish a prepared root. Publication never
replaces an existing destination. Atomic publication requires Darwin or Linux.

Preparation API 1 accepts the normal distfile and patch pipeline. It rejects
custom extraction or patch hooks, alternate distfiles, patch controls and SUBST
source rewrites. It accepts only the canonical recipe metadata variables, three
standard includes and known pre-configure commands. Opaque include files and
extra pre-configure commands fail. The canonical autoreconf step remains supported.
A new source transformation requires an explicit shared preparation contract.

The prepared root contains upstream `src/` and `doc/`, exact `ports.distinfo`,
and a read-only `libedit-source.json`. The receipt binds the clean Ports commit,
recipe metadata, archive hashes, patch hashes and prepared tree inventory.
Read-only permissions prevent accidental receipt edits; they are not a digital
signature. Validation detects changes against the recorded inventory and the
selected clean Ports recipe. Keep the prepared source unchanged during builds.

Both `build-sysroot.py` and `build-os-release.py` require `--libedit-source`.
They validate it before and after producers, pass `-V EMBER_LIBEDIT_SOURCE`,
and bind its identity into object contexts and output receipts. A changed
source or recipe invalidates object reuse. Before accepting output, both
builders require the matching installed distinfo, histedit/readline headers and
a target ELF library with SONAME `libedit.so.3`. Native linker/runtime aliases
must select that one provider. A separate `/usr/pkg/lib/libedit.*` is rejected.
Kernel-only and host-tool commands outside these library/release builders do not require this input.

The OS retains its native libedit ABI and installs `ports.distinfo` as
`/usr/share/emberbsd/ports/editline.distinfo`. Ports checks that identity before
selecting the base library. Source preparation does not qualify that ABI or
prove a target build.

Python callers use `emberbsd_sdk.libedit.prepare_source(ports, archive, output)`
and `validate_source(root, ports=ports)`. Both return the portable receipt.
`LIBEDIT_SOURCE_API` is 1. `validate_identity(receipt)` checks the portable
schema without reading the original host paths.
