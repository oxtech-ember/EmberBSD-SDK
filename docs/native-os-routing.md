# Publish OS native-tool routing

`emberbsd_sdk.native_os_routing` implements routing API version 1.
It publishes the OS-owned `tools/native-tools/contract` through the existing
exclusive directory publication API. It does not start a producer.

The OS interface is available from
[OS commit 387df575](https://github.com/oxtech-ember/EmberBSD/commit/387df575a49e256c6a6d274903583ffb4b5c369e).
Read that source tree's `tools/native-tools/README.md` for its current scope.
The initial OS pilot covers build.sh, make selection, wrapper generation, and
tools-target dispatch. It does not qualify nested configure or generated tools.
SDK sysroot and release execution must remain refused until those routes pass.

Before publication, the producer must:

1. Pin the clean OS source and the contract's exact SHA256.
2. Call `read_native_os_contract` to obtain its required role set.
3. Load the ordinary `NativeToolset` with independently accepted producer evidence
   and those required roles. A bootstrap seed cannot replace that selection.
4. Hold the workspace read leases for the selected tools, host ABI inputs, and
   OS source. Own an output parent outside every input tree.
5. Call `publish_native_os_routing` with the selection, contract path, contract
   hash, and an absent absolute destination below that owned parent.

The published directory contains the exact regular contract file and symlinks
to the selected absolute tool spellings. An in-bundle alias remains an alias.
The emitter does not copy binaries, create wrappers, or canonicalize argv[0].
It refuses the OS interface's unsupported path characters before publication.
An existing destination remains unchanged. Failed publication removes only
the emitter's exact private stage directory.

Invoke `build.sh` with the selected shell's absolute path. Set
`EMBER_NATIVE_TOOLS` to the published directory. Keep the leases, routing
directory, and selected payloads immutable until all children have drained.
The returned mapping record is routing evidence, not tool qualification or
permission to bypass SDK execution gates.

`tests/test_native_os_routing.py` checks publication, selected aliases,
missing roles, unsupported paths, malformed contracts, and no-replace failures.
It never executes its synthetic members or compiles a program.
