# Diagnostic fuzz checks

Use `scripts/fuzz-check.py` to run a prepared host fuzz target and check its
portable evidence. The command uses Python and the standard library. It does
not download, compile, install, publish, or enroll a project in OSS-Fuzz.
Ports owns the source recipe, patches, fuzz harness, seed corpus and build adapter.
SDK owns process control and the common run/evidence contract.

Clang can build diagnostic host fuzz programs with libFuzzer, ASan and UBSan.
These executables must remain outside target packages and sysroots. Build shipped
EmberBSD objects with the selected production toolchain: Clang by default, or
GCC16 for an explicit platform exception. The diagnostic replay uses that same
compiler family. A host replay does not qualify an EmberBSD binary,
ABI, kernel, device or production package.

LeakSanitizer uses ASan and requires an intentional-leak positive control.
The runner keeps the RSS-limited libFuzzer campaign on ASan/UBSan, then replays
each initial and generated corpus file in a separate Clang LSan executable.
This avoids treating libFuzzer's process-lifetime RSS monitor as an application
leak. The LSan replay does not cover mutations that were not retained in either
corpus. Memcheck uses a separate production replay binary without ASan or UBSan. Do
not run an ASan binary under Valgrind.

## Prepare and run

The build adapter writes a version 2 JSON manifest. Use an explicit SDK source
and check `emberbsd_sdk.fuzz_check.FUZZ_CHECK_API == 1` in the adapter.
`FUZZ_MANIFEST_VERSION` is `2`. Existing version 1 GCC replay manifests remain
readable with their original meaning. The module exports `file_sha256(path)` and
`tree_files(root)` to hash inputs. The latter returns sorted `{path, sha256}`
records with relative paths. It refuses symlinks and special files.

```sh
python3 scripts/fuzz-check.py run \
  --manifest /absolute/cairo-build/build.json \
  --output /absolute/cairo-check \
  --runs 10000 --seed 1 --max-len 65536 \
  --timeout 300 --input-timeout 10 --rss-limit-mb 2048 \
  --memcheck optional
python3 scripts/fuzz-check.py verify --evidence /absolute/cairo-check \
  --manifest /absolute/cairo-build/build.json
```

The output directory must not exist or overlap an input. `--timeout` bounds the
fuzzer process in seconds. `--input-timeout` bounds a libFuzzer input and each
production replay process. The SDK session backend drains owned child processes on
failure or timeout. A failed run retains `INCOMPLETE` and its available logs.
Do not treat a failed run or a successful process without a libFuzzer completion
record as accepted. Run budgets are finite; a short run is not a soak test.

The runner copies the initial seeds into its working corpus. It runs libFuzzer
with the declared seed, run count, maximum input length and memory budget. It
then executes the production replay binary once per file in both the initial and
final corpus. The replay binary receives one filename as its only argument.
It must execute the same harness on all bytes in that file and return nonzero
on failure. Keep empty inputs in the seed corpus if that boundary matters.
The fuzzer must use the standard libFuzzer command-line interface.

When `ASAN_OPTIONS` enables leak detection, the runner first executes
`binaries.lsan_probe` and requires a LeakSanitizer report for its intentional
allocation. This positive control proves that the selected host runtime detects
the test leak. The runner then executes `binaries.lsan_replay` once for every
initial and generated corpus file with leak detection enabled. A nonzero replay
fails the run. The RSS-limited libFuzzer and production ASan/UBSan replays use
`detect_leaks=0`; their retained inputs are covered by the separate LSan replay.

`--memcheck off` skips Valgrind. `--memcheck optional` records `unsupported` or
`unavailable` when the host cannot run it and does not call that a pass.
`--memcheck required` refuses to start unless Valgrind is available on a
supported platform. A Memcheck manifest supplies a separate
`binaries.memcheck` replay built with the production compiler and without sanitizers. The runner
uses full leak reports, origin tracking and a failing exit code for definite,
indirect and possible leaks or memory errors. It replays every initial and
generated corpus file. Still-reachable allocations are reported but are not
treated as lost memory. The evidence records the Valgrind version, binary hash,
commands, XML reports, statuses and hashes.

All executed argv arrays, environment settings, statuses and output logs are
retained. Output capture is bounded; truncation fails the run. Process controls
reuse the SDK host backend. This interface does not establish native Windows
support or isolate arbitrary hostile executables. Use trusted local build
adapters and a suitable external sandbox for untrusted code.

## Manifest version 2

Every object rejects unknown or missing keys. SHA256 strings contain 64 lower
case hexadecimal characters. Artifact paths are absolute. Corpus entry paths
are relative, unique and sorted. JSON duplicate keys and nonfinite numbers are
rejected. Required top-level fields are:

| Field | Value |
| --- | --- |
| `schema` | Integer `2` |
| `name` | Nonempty target name |
| `scope` | `diagnostic-host` |
| `host` | `{system, machine}` from Python `platform` |
| `sources` | Nonempty list of `{name, revision, path, sha256}` |
| `tools` | Required `clang` and `replay`; optional `clang_lsan` and `memcheck`; each `{path, sha256, family, version, flags}` |
| `binaries` | Required `fuzzer` and `replay`; optional `lsan_probe`, `lsan_replay` and `memcheck`; each `{path, sha256}` |
| `corpus` | `{path, files}`, where files are `{path, sha256}` records |
| `build_logs` | Nonempty list of `{path, sha256}` |
| `limitations` | Nonempty list of explicit scope limits |
| `sanitizer_options` | `ASAN_OPTIONS` and `UBSAN_OPTIONS` below |

The adapter must enumerate every prepared source file, archive, applied patch,
harness and build configuration used in this build. Include generated headers
and dependency provenance. Use unique source names and paths. `revision` names
the applicable immutable source revision or archive identity. Build logs must
include actual compiler argv and configuration results. Compiler versions and
flags describe the real build; version text alone cannot prove which compiler
produced an executable. The runner checks recorded file bytes before and after
execution. It cannot discover omitted build inputs or infer a complete source
closure from a caller's manifest. Review and test the owning adapter.

Compiler paths must refer to real files, not symlinks. Resolve a selected
compiler before hashing it. Each tool declares `family: "clang"` or `"gcc"`.
`clang` and `clang_lsan` require Clang; `replay` records the selected production
family. GCC versions must be 16.x. Compiler flags are
nonempty arrays. `clang_lsan` records the positive-control build flags;
`memcheck` must match the replay family and version and have no `-fsanitize`
flag. Version 1 retains its `gcc` and `gcc_memcheck` names and has no `family`
field; these records always mean GCC16. Target builds continue to require
the normal compiler, sysroot and release validation independently.

`ASAN_OPTIONS` is exactly `abort_on_error=1:detect_leaks=1` with both
`lsan_probe` and `lsan_replay`, or `abort_on_error=1:detect_leaks=0` with an
explicit leak-check limitation. A positive control without LSan replay is not
a LeakSanitizer result.
`UBSAN_OPTIONS` is exactly `halt_on_error=1:print_stacktrace=1`.
The runner removes inherited loader and sanitizer overrides. It retains only
`PATH`, `HOME`, `TMPDIR` and `LANG`, then adds these explicit options. A disabled
leak detector is not a leak-check pass. The [Valgrind platform matrix](https://valgrind.org/info/platforms.html)
lists Linux, FreeBSD and older Intel macOS targets; it does not list current
macOS arm64 or a current NetBSD target. Keep unsupported runtime or dependency
paths in the manifest limitations. An optional Memcheck result of
`unsupported` or `unavailable` is not a pass. Memcheck's [official manual](https://valgrind.org/docs/manual/mc-manual.html)
describes the checked memory errors and leak classes.

## Check evidence

A successful run prints the SHA256 of `evidence.json`. The directory contains
copied binaries, seed and final corpora, build logs, the manifest snapshot,
command records and exact file hashes. New runs use evidence schema 3. It checks
LSan positive-control output and every LSan corpus replay, plus Memcheck mode,
tool identity, XML reports and corpus coverage. The verifier continues to
accept existing schema 1 and 2 receipts; old schema 1 receipts cannot attest
LSan or Memcheck. Both `run` and `verify` return nonzero on refusal.

```sh
python3 scripts/fuzz-check.py verify --evidence /absolute/cairo-check
python3 scripts/fuzz-check.py verify --evidence /absolute/cairo-check \
  --sha256 TRUSTED_EVIDENCE_JSON_SHA256
```

Offline verification survives relocation and removal of the original build.
It establishes internal consistency only. These unsigned files are not an
attestation of origin, a signature, a formal proof, or protection against an
actor who replaces both payload and hashes. Obtain `--sha256` from a separate
trusted channel if its identity matters. Copied executables can still depend
on host shared libraries; retaining their bytes does not make the evidence a
self-contained runtime distribution.

Add `--manifest` to compare the snapshot with an explicit current build and
rehash its recorded source, tool, binary, build-log and corpus inputs. This
rejects stale or modified recorded inputs. It does not detect unlisted files;
regenerate the manifest with the owning build adapter when its input set
changes. Keep exact evidence outside the source tree. Do not confuse a passing
finite campaign with proof of absence of defects.

## Check the runner

```sh
python3 -m unittest discover -s tests -p 'test_fuzz_check.py'
```

The contracts use host subprocess fixtures for failure, timeout, replay, LSan
positive-control and per-input replay handling, and Memcheck evidence. They also
check stale artifacts, corpus omission, evidence tampering and offline
relocation. Fixtures do not substitute for real sanitizer or Valgrind execution,
or a real library fuzz campaign.
