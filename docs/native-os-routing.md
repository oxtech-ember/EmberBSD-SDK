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
