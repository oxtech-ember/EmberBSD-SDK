# Route native tools to the OS build

`emberbsd_sdk.native_os_routing` implements routing API version 1.
It publishes the selected OS `tools/native-tools/contract` as a command mapping.
Read that source tree's `tools/native-tools/README.md` for the OS interface.

Sysroot and release builders use this sequence:

1. Read the OS contract and obtain its required tool roles.
2. Load those roles and Python from `--host-prefix`.
3. Lock the output contexts and publish the mapping under the tools directory.
4. Invoke `build.sh` with the selected shell and `EMBER_NATIVE_TOOLS`.

The mapping contains the contract and symbolic links to selected absolute paths.
An internal compiler alias keeps its spelling because drivers can use their invocation path.
The OS uses the mapped NetBSD make, shell, utilities, and host compiler variables.
SDK does not pass conflicting `HOST_CC`, `HOST_CXX`, or `HOST_SH` command overrides.
Native ABI flags remain separate from target flags.

The mapping is ordinary command routing. It needs no independent acceptance.
Keep tools unchanged while a build runs. An existing mapping must match the selected prefix.
The emitter refuses missing roles, invalid paths, and an existing output destination.
Failed publication removes only its own temporary directory.
The contract hash checks the source copy; it does not qualify a toolchain.

The direct API is `publish_native_os_routing(toolset, *, contract_path,
contract_sha256, destination)`, where `toolset` comes from `load_host_prefix`.
`read_native_os_contract(path, *, sha256)` returns bytes, rows, and required roles.
The builders compute the contract hash from their selected OS source.
Users do not supply a separate contract receipt.

`tests/test_native_os_routing.py` checks publication and refusal cases.
`tests/test_native_consumers.py` checks prefix selection and producer wiring.

The OS contract can bind selected `cp` and `rm` roles to `HOST_MV_CP` and
`HOST_MV_RM`. Host `mv` uses these fixed children for cross-device moves.
Missing roles fail mapping publication before any tool runs.

The sysroot and release builders accept an optional `--byacc-source` prepared
by the common Ports byacc source owner. When selected, they verify its prepared
tree and pass `EMBER_BYACC_SOURCE` to OS producers. Before building, the SDK
queries the OS base yacc and host yacc makefiles and requires each effective
`IDIST` to select that source. Without the option, older OS revisions retain
their existing behavior.

The optional `--texinfo-source` selects the Ports prepared Texinfo source for
the OS C-only producer and passes `EMBER_TEXINFO_SOURCE`. The SDK queries the
actual producer for its effective source and C-only configure option; older OS
revisions that do not select this source are refused when the option is used.
The selected host prefix provides GNU make, and the checked native compiler
provides the GNU build triple for this target producer. Its omitted Perl test
fixture makes it unsuitable for a full Perl/text producer. Native makeinfo and
install-info come from the selected host prefix.

For common libarchive, libevent, byacc and Texinfo sources prepared with package-scoped
identity, select `--source-patch` inside the host prefix. To compare libevent
byacc or Texinfo records when source inputs changed, also select
the corresponding `--libevent-archive`, `--byacc-archive` or `--texinfo-archive`; the owner checks the
archive and full patch set before comparing the prepared payload.

The optional `--rcs-source` selects the common Ports RCS source for the OS
producer, with the same checked GNU make and native compiler triplet. The SDK
queries the producer's effective source and passes `EMBER_RCS_SOURCE` to the
sysroot and release commands. When changed prepared inputs need comparison,
provide `--rcs-archive` and `--source-patch`; the owner uses the selected
prefix lzip to compare the full prepared payload.

The optional `--diffutils-source` selects the common Ports diffutils source
for the OS producer and passes `EMBER_DIFFUTILS_SOURCE` to sysroot, release,
and set checks. GNU make and the native compiler triplet are selected from the
same host prefix. For changed source inputs, `--diffutils-archive` and
`--source-patch` allow the owner to compare a freshly prepared payload.

The optional `--grep-source` selects the common Ports GNU grep source for
sysroot, release and set checks. It uses the selected native GNU make and
passes `EMBER_GREP_SOURCE` to the OS producer. When source inputs change,
`--grep-archive` and `--source-patch` permit a fresh payload comparison.

The optional `--ekermit-source` selects the common Ports E-Kermit source for
the OS producer and passes `EMBER_EKERMIT_SOURCE` to sysroot, release and set
checks. When source inputs changed, `--ekermit-archive` and `--source-patch`
allow the owner to compare the prepared payload.

The optional `--tdb-source` selects the prepared common Ports TDB source for
OS headers, the target library and release/set producers. The selected host
Python is passed only for this Waf producer. When preparation inputs change,
`--tdb-archive` and `--source-patch` let the owner compare the full prepared
payload. The OS producer supplies its target configuration independently of
the machine used for runtime tests.

With `--source-patch` and a Ports revision that provides
`scripts/pkgsrc-shared-export.py`, the SDK exports pkgsrc once per process
for the libarchive, libevent, byacc, texinfo, RCS, diffutils, grep,
E-Kermit, nvi and tcpdump owners (preflight, the checks before and after the build, and
every LLVM phase) and passes it as `--pkgsrc-export`. The export is keyed by
the Ports commit, the pinned pkgsrc commit and the patch tool; each owner
also checks it against its current Ports inputs. Older Ports revisions keep
one export per owner.

`--source-receipts DIR` keeps each verified prepared-source record in `DIR`
for later runs. An entry is reused only when the owner command, the
environment (except `TMPDIR`), the checked Ports and SDK commits and the
metadata fingerprint of every input still match; otherwise the owner verifies
the source again and the entry is replaced. A reused record needs no pkgsrc
export, so the shared export is made only when an owner must run. Without
the option, records live only for the current process.

The sysroot builder accepts `--llvm-objects` and `--llvm-stages` to select
LLVM coordinator subdirectories inside the existing SDK's `obj` and `stages`
directories when a different build context must be retained alongside an
existing one. Existing contexts and stages are not overwritten.

Without `--llvm-stages`, the headers and base stages are kept in a
subdirectory of `stages/llvm-bootstrap-TARGET`. The bootstrap and full
runtime stages use the same name below `--runtime-work`. When both OS phases
of a stage set complete, the coordinator writes `inputs.json` beside them:
the OS directories that the stage build read, taken from the stage's own
build rather than from a list of consumers:

* every directory with outputs in the stage object trees and every directory
  named by a compiler dependency file there;
* every OS path that the stage build log names, except the sources of host
  tools that the stage never runs (the stage builds all host tools, but runs
  only some; config(1), for example, is never run);
* for the host tools that the log runs, matched to their object directories
  by the installed program's content, those directories and their dependency
  files, and always bmake. If a program that the log runs has no matching
  object directory, every host tool counts as an input;
* build.sh, the top Makefile, share/mk, etc/mtree, tools/, every shared
  makefile fragment, and every header and makefile below common, crypto,
  external, gnu, include, lib and sys.

The direct files of each listed directory and of its parent directories are
included, so a new file in a used directory is an input. The record's key is
a hash of the Git blob ids and modes of those files, the LLVM source record
and the compiler record. A later run selects the first recorded stage set
whose key, computed at the new OS commit, is unchanged, so an OS change that
the stage never reads, such as usr.bin/config, reuses the completed stages.
Otherwise the run uses a stage set named by a hash of the whole OS source
tree (not the commit), the LLVM source record and the compiler record, which
builds new stages incrementally from the shared `obj/llvm-bootstrap`
objects; a new commit with the same tree reuses them. Each stage is still
checked against its receipt before reuse: its recorded inputs, or without a
record its whole OS tree, must be identical at the built and the current
commit. Older stage subdirectories are kept; remove unused ones when no
build uses them. An explicit `--llvm-stages` is used as given.

A development run continues after a failed run from the failed stage. Completed
stages are reused as above. A headers or base stage that the failed run left
incomplete with the same objects, or a runtime stage without its
`runtime.json`, is moved with its log and work tree to `failed-N` next to it
for inspection, and that stage is built again. `build-os-release.py`, and any
explicit `--llvm-stages` directory, keep refusing an incomplete stage: a
publishable release never continues after a failed stage.

The release builder also accepts `--x11-source`, the tree prepared by the Ports
`scripts/source-inputs/xsrc` owner from the NetBSD 11.0 `xsrc.tgz` set. It is
valid only for the Clang `aarch64-sysroot` profile. The SDK verifies the
prepared tree through that owner and requires its archive URL and SHA512 to
equal `x11SourceUrl` and `x11SourceSha512` in the OS `ember/source.json`. The
profile then sets `MKX11=yes` and `X11SRCDIR`, adds `xbase xcomp xetc xfont
xserver` to `MAKETARSETS`, and requires those sets as outputs. The xsrc
identity is part of the native tools context because OS tools build X11 font
tools from it. Without the option the profile keeps `MKX11=no`.

The optional `--nvi-source` selects the common Ports nvi source for the
native OS producer. `--nvi-archive` and `--source-patch` support verification
when the effective Ports inputs change. Native nvi uses the base DB1 API;
the separate package uses Berkeley DB 18.

The optional `--ppp-source` selects the common Ports PPP source for the OS
producer. `--ppp-archive` and `--source-patch` permit verification against
changed recipe inputs.

The optional `--top-source` selects the common Ports top source for the OS
producer. `--top-archive` and `--source-patch` support changed-input comparison.

The optional `--tcp-wrappers-source` selects the common Ports TCP wrappers
source for native headers, library and consumers. `--tcp-wrappers-archive`
and `--source-patch` support verification after owner inputs change.

The optional `--libpcap-source` selects the common Ports stable libpcap source for
native headers, library and release producers. `--libpcap-archive` and
`--source-patch` support verification when the effective inputs change. The
stable native library uses SONAME 12; consumers of SONAME 10 or 11 must be
relinked.

The optional `--tcpdump-source` selects the shared Ports tcpdump source for
the native OS producer. `--tcpdump-archive` and `--source-patch` support
verification when effective inputs change. The native program retains its
OS installation policy and links against the selected native libpcap.

Select `--ntp-source` and `--libopts-source` together for the native NTP
producer. The former is prepared by the common Ports NTP owner and the latter
by the common AutoGen owner. `--source-patch` selects the host patch tool;
`--ntp-archive` supports comparison when NTP inputs change. AutoGen source
verification does not require its host package to remain installed after the
tearoff has been prepared.

Select `--gettext-source` and `--libxml2-source` together for the native
gettext tools. Their Ports owners verify the prepared inputs; the corresponding
`--gettext-archive`, `--libxml2-archive` and `--source-patch` options support
comparison after recipe inputs change. The release builds the ordinary target
foundation first, then prepares the shared Ports cross-libtool in its OS
TOOLDIR before building the gettext and static libxml2 consumers. This path
uses the selected native tools and target runtime; it does not add GNU libintl
to the native Citrus ABI. The OS base selects plain output without the newer
color and CSS features.

A successfully inspected release whose profile publication was interrupted can
be finalized without rebuilding: run `scripts/build-os-release.py finalize
--sdk-source /absolute/sdk-source --destination /absolute/release-stage` with
the selected host Python. This verifies the saved producer results and inputs;
it does not resume a failed or incomplete release. The current finalizer
supports inspected Clang/LLVM releases.
