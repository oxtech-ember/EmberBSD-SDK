# Prepare the canonical libelf source

Ports owns the elftoolchain 0.7.1 recipe, upstream archive and every source
patch in `pkgsrc/devel/elftoolchain`. The SDK prepares only the patched
`common/` and `libelf/` trees for OS consumers. It does not prepare or qualify
the separate `libdwarf` source tree.

Use a clean, fully materialized Ports checkout and the original archive bytes.
Select a new output path with an existing parent:

```sh
python3 scripts/prepare-libelf.py \
  --ports /absolute/EmberBSD-Ports \
  --archive /absolute/elftoolchain-0.7.1.tar.bz2 \
  --output /absolute/prepared-libelf
python3 scripts/prepare-libelf.py \
  --validate /absolute/prepared-libelf \
  --ports /absolute/EmberBSD-Ports
```

`--ports` is optional for validation of a prepared root. Without it, validation
checks the portable receipt and source bytes. With it, validation also requires
the selected clean Ports commit, recipe, manifest, distinfo and patch set to
match. Preparation requires Git and `patch` on `PATH`. Python uses only its
standard library. This source operation does not compile or install software.

The preparer checks the archive's SHA256, SHA512, BLAKE2s and size against the
recipe manifest and `distinfo`. It checks every patch SHA1 with pkgsrc's RCS
line filtering rule and records each raw SHA256. It applies all ordinary
patches in lexical order. It rejects custom extraction or source-changing
hooks, alternate archives, unsupported recipe variables, unsafe paths,
links, special files and unexpected source files. The manifest binds the
expected `common/` and `libelf/` contents to the recorded EmberBSD base
revision. The two recipe-owned packaging overrides retain their base hashes
and state their reason.

Patch application uses BSD patch options and sends any backup files to a fresh
temporary directory for that patch. The temporary directory is outside the
prepared root. The preparer verifies the final tree before it publishes a new
output directory. Publication never replaces an existing path.
Atomic directory publication currently requires Darwin or Linux; other hosts
fail closed until their no-replace primitive is qualified.

The output has sibling `common/` and `libelf/` directories, exact recipe
`ports.distinfo`, and a read-only `libelf-source.json`. The receipt records the
clean Ports commit, manifest and recipe hashes, upstream archive and patch
identities, base revision, exact prepared file inventory and tree hash. It
contains no local checkout paths. The SDK API can validate this generated
snapshot later without the Ports checkout. Preparation publishes the receipt as
mode `0444`. Git stores non-executable files as mode `100644`, so archive and
checkout operations create it as mode `0644`. Validation accepts these two
receipt modes and still checks the receipt data, source bytes, source modes,
inventory and tree hash. Read-only permissions prevent accidental edits; they
are not a signature.

Python callers use `emberbsd_sdk.libelf.prepare_source(ports, archive, output)`
and `validate_source(root, ports=ports)`. Both return the portable receipt.
`LIBELF_SOURCE_API` is 1. `validate_identity(receipt)` checks the receipt schema
without accessing the original host.

Source preparation does not prove the OS ABI, a target build, or Plasma
hardware acceleration. OS source rules retain ownership of the native ABI and
library installation.
