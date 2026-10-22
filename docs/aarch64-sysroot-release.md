# AArch64 development-set receipt

The [release tool](os-release.md) composes complete `base.tar.xz`, `comp.tar.xz`
and `netbsd-EMBER64.gz` from one OS build. SDK owns composition and publication;
OS owns the sets and kernel producers. Ports owns the GCC16 consumer/import
contract. These development sets do not qualify installation media or boards.

## Target qualification

Use an empty sysroot. Check libc aliases and actual loader/runtime selection.
Build the current Ports C11 plain/LTO, atomics, TLS and thread consumers, plus
C++20 and cross-DSO exception checks. Execute them with the matching kernel and
base. Run the fork CAS and bounded libc/loader regressions against those inputs.
The target needs no compiler. Record actual library paths and SHA256 values.

A qualification JSON binds `source_commit`, `base_sha256`, `comp_sha256` and
`kernel_gzip_sha256`. It records the measured GCC16.2.0 banner in
`consumer_compiler` and the execution platform as
`EmberBSD aarch64 full-system QEMU TCG`.
Its `checks` object must contain true results for `libc_aliases`, `cross_c11`,
`cross_cxx20`, `target_c11`, `target_cxx20`, `base_loader_smoke` and `fork_cas`.
Record true only from actual successful execution.

Keep the raw target serial evidence in the build directory as
`target-acceptance.log`. Bind its SHA256 in the receipt's `evidence_sha256`
object under that filename. The kit includes and verifies the bound log.
`runtime_libraries` can record observed providers. Preparation removes unrelated
receipt fields; do not include credentials or personal data.

```sh
python3 scripts/os-release.py --profile aarch64-sysroot \
  prepare /absolute/build /absolute/new-qualified-kit \
  --qualification /absolute/target-qualification.json
python3 scripts/os-release.py --profile aarch64-sysroot verify /absolute/new-qualified-kit
```

External CI pins the full OS source and accepted manifest SHA256.
Use the Ports `profiles/common-build-tools/cross/fetch-sysroot.py` importer with
an explicit SDK source. It checks HTTPS, hashes, paths and provenance before
creating a new sysroot. GCC16 packages and compiler remain separate inputs.
Do not extract development sets over a live system.

## Preserved historical evidence

The schema 1 source is `6dd57144183678938164b4c2dcfc370cdbde8473`.
Its [complete producer](https://github.com/oxtech-ember/EmberBSD/actions/runs/38843939746)
used the in-tree GCC12.5 bootstrap on Ubuntu 24.04. Native compiler commands
were omitted; GCC16.2 consumers were tested separately. This receipt does not
qualify the new common-GCC16 release producer.

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
