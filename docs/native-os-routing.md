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
revisions that do not select this source are refused when the option is used. Its omitted Perl test
fixture makes it unsuitable for a full Perl/text producer. Native makeinfo and
install-info come from the selected host prefix.

For common libarchive, libevent, byacc and Texinfo sources prepared with package-scoped
identity, select `--source-patch` inside the host prefix. To compare libevent
byacc or Texinfo records when source inputs changed, also select
the corresponding `--libevent-archive`, `--byacc-archive` or `--texinfo-archive`; the owner checks the
archive and full patch set before comparing the prepared payload.

The sysroot builder accepts `--llvm-objects` and `--llvm-stages` to select
LLVM coordinator subdirectories inside the existing SDK's `obj` and `stages`
directories when a different build context must be retained alongside an
existing one. Existing contexts and stages are not overwritten.

The release builder also accepts `--x11-source`, the tree prepared by the Ports
`scripts/source-inputs/xsrc` owner from the NetBSD 11.0 `xsrc.tgz` set. It is
valid only for the Clang `aarch64-sysroot` profile. The SDK verifies the
prepared tree through that owner and requires its archive URL and SHA512 to
equal `x11SourceUrl` and `x11SourceSha512` in the OS `ember/source.json`. The
profile then sets `MKX11=yes` and `X11SRCDIR`, adds `xbase xcomp xetc xfont
xserver` to `MAKETARSETS`, and requires those sets as outputs. The xsrc
identity is part of the native tools context because OS tools build X11 font
tools from it. Without the option the profile keeps `MKX11=no`.
