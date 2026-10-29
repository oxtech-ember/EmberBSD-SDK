# AArch64 development-set receipt

The [release tool](os-release.md) composes complete `base.tar.xz`, `comp.tar.xz`
and `netbsd-EMBER64.gz` from one OS build. SDK owns composition and publication;
OS owns the sets and kernel producers. Ports owns the selected compiler consumer/import
contract. These development sets do not qualify installation media or boards.

## Target qualification

This receipt is optional. Publication does not require it.
Use an empty sysroot. Check libc aliases and actual loader/runtime selection.
Build the current Ports C11 plain/LTO, atomics, TLS and thread consumers, plus
C++20 and cross-DSO exception checks. Execute them with the matching kernel and
base. Run the fork CAS and bounded libc/loader regressions against those inputs.
The target needs no compiler. Record actual library paths and SHA256 values.

A qualification JSON binds `source_commit`, `base_sha256`, `comp_sha256` and
`kernel_gzip_sha256`. For Clang schema 4 or 5 build profiles, record
`consumer_compiler_family: "clang"` and the measured Clang banner in
`consumer_compiler`, with the same version as the SDK build receipt.
Schema 5 selects the common Clang C++ runtime in the OS base set. Schema 4
retains the historical build-only runtime selection and command sequence.
Both produce the existing `emberbsd.aarch64.sysroot.v3` outer kit manifest;
this version describes the kit format, not target acceptance. The explicit
GCC route and historical schemas retain the GCC16.2.0 consumer identity.
Record the execution platform as
`EmberBSD aarch64 full-system QEMU TCG`.
Its `checks` object must contain true results for `libc_aliases`, `cross_c11`,
`cross_cxx20`, `target_c11`, `target_cxx20`, `base_loader_smoke` and `fork_cas`.
Record true only from actual successful execution. Current
`target-runtime-v2` qualifications also require `libm_valid` and
`setjmp_valid` and bind the OS test and Ports source commits and test-source
inventory hash. Earlier qualifications without a corpus marker are v1; they
remain readable but do not establish these two checks.

Run the shared SDK [target acceptance runner](target-acceptance.md). It writes
`qualification.json` and the raw `target-acceptance.log` together in its new
output directory. Preparation reads that evidence sidecar and leaves the OS
build output untouched. The kit includes and verifies the bound log.
`runtime_libraries` can record observed providers. Preparation removes unrelated
receipt fields; do not include credentials or personal data.

```sh
/absolute/native-prefix/bin/python3.15 scripts/os-release.py --profile aarch64-sysroot \
  prepare /absolute/build /absolute/new-qualified-kit \
  --qualification /absolute/acceptance-output/qualification.json
/absolute/native-prefix/bin/python3.15 scripts/os-release.py --profile aarch64-sysroot verify /absolute/new-qualified-kit
```

External CI pins the full OS source and accepted manifest SHA256.
Use the Ports `profiles/common-build-tools/cross/fetch-sysroot.py` importer with
an explicit SDK source. It checks HTTPS, hashes, paths and provenance before
creating a new sysroot. The selected compiler and matching Ports runtime
packages remain separate inputs.
Do not extract development sets over a live system.

## Preserved historical evidence

The schema 1 source is `6dd57144183678938164b4c2dcfc370cdbde8473`.
Its [complete producer](https://github.com/oxtech-ember/EmberBSD/actions/runs/38843939746)
used the in-tree GCC12.5 bootstrap on Ubuntu 24.04. Native compiler commands
were omitted; GCC16.2 consumers were tested separately. This receipt does not
qualify the current Clang/LLVM release producer or the explicit GCC alternative.

| Artifact | SHA256 |
| --- | --- |
| `base.tar.xz` | `688dc6701cd83283d1287fc18dee8b9af35f067096cdd4a92de7e0593fdacddb` |
| `comp.tar.xz` | `8c813303f0500ae1a4ba6381eee6a1189ffab86036963fb26f36ec313e02daa6` |
| `netbsd-EMBER64.gz` | `fbc6fa0afe2a91e7798734f58a326d18ccb20bb46e20b1f3579359264e68ea90` |
| Prepared kit manifest | `efc1cd827598451b00b7d743b35a4901a3fd25cf24191b3b3bb679d53f068f5b` |

The finite QEMU TCG check used Cortex-A72, two CPUs and 1 GiB RAM. It checked
8188 archive entries, 5880 regular METALOG files and four fork libc aliases.
C11, C++20, threads, LTO and cross-DSO exception checks passed with the recorded
GCC16 providers. Shared and static libc each passed 850 CAS checks. GNU M4
1.4.21 passed 236 invocations; seven optional `changeword` cases were skipped.
The receipt binds the raw serial log. This is finite consumer acceptance,
not all libc/base commands, Valgrind, installation media, physical hardware or
cross-host bit-for-bit reproducibility. The prepared kit was not a public
binary release; a retained CI artifact alone does not establish publication.
