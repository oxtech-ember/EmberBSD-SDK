# Darwin platform runtime audit

`emberbsd_sdk.darwin_runtime` provides `DARWIN_RUNTIME_AUDIT_API = 1`.
It inspects explicit Mach-O files and a dyld shared cache.
It does not run a compiler, configure script, tool, or producer.
It does not qualify a bootstrap seed or an ordinary native toolset.

Use this API for the separately scoped bootstrap dynamic-dependency audit.
Keep ordinary native-toolset validation unchanged.
The descriptor must still pass the native payload, provenance, ABI input,
driver, and accepted-receipt checks at its owning admission boundary.
This API checks runtime metadata and bytes only.
The native C/C++ compile, link, execution, and observed DSO checks remain required.
It does not implement a Mach-O symbol binder or establish general ABI compatibility.

## Explicit inputs

Pass a descriptor with the selected native `tools`, `bundle_root`, `host`,
and explicit `host_abi.libraries` roots.
Select the canonical main cache file with `cache_main`.
Its parent directory must be exactly one declared ABI library root.
Use that root's existing SDK inventory reference.
Keep SDK headers and link-library stubs as separate ABI inputs.
Do not replace a host SDK with the runtime cache.

The reader checks the complete cache-directory inventory against current bytes.
It streams file hashes through the existing SDK inventory API.
It does not extract images or copy cache files.
Keep all input roots immutable until producer drainage completes.
Do not update the host OS or change its selected cache during that interval.

The reader validates the main cache UUID and every mapped subcache UUID.
It checks the suffix table, VM offsets, file mappings, and image table bounds.
It resolves install-name aliases through the dylib trie.
The image table alone is not an alias index in current caches.
It then checks each reached Mach-O identity, load command, and segment file span.
Cache inventory hashes cover the complete files, including shared LINKEDIT bytes.
The `.map` file is inventoried if present. It is not the path-resolution authority.
Unmapped symbols files are not dependency graph nodes.

`validate_darwin_runtime_closure` also queries the loaded OS dyld service.
It uses `_dyld_get_shared_cache_uuid` from the current process.
It does not search for a library or execute a command.
The live UUID must match the declared main cache UUID.
An unavailable service, absent cache, or UUID mismatch is a refusal.
Offline capture does not perform this live admission check.

## API and records

`capture_darwin_runtime_audit(descriptor, *, cache_main)` returns an audit object.
It starts at the canonical files of all selected native tool roles.
It follows the reached file and cache-image dependency graph.
It does not treat unrelated bundle dylibs as graph roots.
It preserves cache aliases as requested install-name nodes.

Write the audit outside the immutable input roots.
Create its normal SDK `{path, sha256}` reference.
Pass the object and reference to `darwin_runtime_closure(audit, audit_reference)`.
It returns the dynamic-dependency map for the scoped bootstrap consumer.
File rows retain their existing form:

```json
{"sha256": "FILE_SHA256", "dependencies": ["/absolute/node"]}
```

A cache-image row has this form:

```json
{
  "cache_image": {"path": "/absolute/runtime-audit.json", "sha256": "AUDIT_SHA256"},
  "dependencies": ["/absolute/install-name-or-file"]
}
```

Its map key is the exact absolute install name requested by a load command.
All cache-image rows must use one identical audit reference.
The audit format is `emberbsd-darwin-runtime-audit`, version 1, API 1.
It contains the host, cache inventory and UUIDs, canonical tool roots, and nodes.
Each node records its Mach-O UUID, CPU, build platform, minimum OS, raw versions,
load-command hash, segment metadata, and ordered dependency edges.
Lazy-load blobs retain their flags, symbol names, offsets, and content hashes.
A fat file node adds `macho_slice` with its format, CPU, subtype, offset, size,
and alignment. Its file SHA256 still covers the complete wrapper and padding.
A resolved `@rpath` edge adds `rpath_resolution` with its owner and candidates.
The original load name remains unchanged. `target` is the canonical selected file.

`validate_darwin_runtime_closure(value, descriptor)` reads the pinned audit.
It reconstructs the entire audit from current inputs.
It requires exact audit and dependency-map equality.
It refuses an omitted edge, changed file, different alias, or extra graph node.
It does not turn a missing ordinary file into a cache-image node.
The bootstrap consumer must select this API only for an explicit `cache_image` row.

## Supported metadata and refusals

The reader supports thin little-endian 64-bit Mach-O images.
An on-disk shared library can also have one big-endian fat32 wrapper.
That wrapper must contain exactly one generic arm64 slice with subtype 0.
Its table and thin MH_DYLIB identity must agree.
Offsets, alignment, and metadata reads must stay inside that slice.
Multi-slice files, other fat encodings, and fat executables or objects fail explicitly.
It checks the declared host CPU and requires a macOS build platform.
The minimum macOS version cannot exceed the cache OS version.
It supports the modern cache suffix table and the published header prefix.
Older cache layouts, embedded dyld environment commands, and unknown
load-command encodings fail explicitly.

Dependency install names can be absolute or use the restricted `@rpath` form below.
Other relative names and direct `@loader_path` or `@executable_path` loads fail.
Recorded `LC_RPATH` strings do not cause a search for an absolute dependency.
An ordinary file target must stay inside a declared bundle or ABI library root.
There is no system-library name allowlist or missing-file fallback.

### Image-local rpaths

The supported form is `@rpath/leaf`, with one filename component.
Only an on-disk image inside `bundle_root` can request it.
The reader uses that image's own `LC_RPATH` entries in their recorded order.
An entry can be an absolute in-bundle directory or `@loader_path` with an
optional relative suffix. `@loader_path` refers to the directory of the image
that owns this `LC_RPATH`, not the executable or another requesting image.
Empty, `.` and `..` suffix components fail.
Each anchor and existing target must have its real in-bundle spelling.
An image with an aliased selected or dependent load path cannot own these edges.
This avoids guessing a symlink-dependent loader anchor.

Exactly one distinct existing target must match these local candidates.
Multiple different files fail, even when their bytes match.
The reader retains all candidate paths and checks missing candidates again.
An appearing candidate invalidates the audit.
Each image supports at most 64 `LC_RPATH` entries for this resolution.
The complete audit supports at most 4096 rpath candidates, including repeats.

No environment, working-directory, default-library, or external search is used.
Inherited rpaths and `@executable_path` expansion are not implemented.
A missing own-image match fails, including a weak request.
The reader does not convert it to a null weak edge or try an inherited path.
Cached-image and lazy-blob `@rpath` requests remain unsupported.
Use this subset for an in-bundle `@rpath` library chain.
They do not create an output-artifact admission interface.
Native tool roots and bootstrap acceptance remain unchanged.

The reader preserves strong, weak, re-export, upward, delayed, and lazy edges.
It supports the published `dylib_use_command` attribute encoding.
It reads `LC_LAZY_LOAD_DYLIB_INFO` from its bounded LINKEDIT blob.
Each lazy-load blob supports at most 4096 symbols.
The complete audit supports at most 1 MiB of decoded UTF-8 symbol bytes.
Repeated symbol offsets consume the budget for each decoded occurrence.
The reader refuses larger inputs. It does not truncate symbol lists.
It records dyld's implicit libSystem edge separately from explicit load commands.
Missing strong, re-export, upward, and nonweak lazy targets fail.
A missing weak target remains an explicit edge with `target: null` in the audit.
Its full command or lazy encoding, name, and raw flags remain present.
The reader checks absence in the cache and on disk, including after content checks.
A target that appears later invalidates validation.
It never drops the weak command or silently selects another library.
A regular cached-image weak edge that leaves the cache cannot select a disk file.
Current dyld breaks that edge. This API refuses it if the disk file exists.
It supports the null edge only after verified absence from both inputs.

The audit preserves dylib current and compatibility versions.
It does not invent a version inequality that current dyld does not enforce.
It preserves the full lazy-load flags field and interprets its published weak bit.
The fixed native execution checks and the producer's DSO audit remain mandatory.

## Source basis and checks

The implementation is original Python code based on these Apple format contracts.
No Apple parser implementation is included in this package.
The source files retain their Apple Public Source License at their owner.

- [Cache structures](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/include/mach-o/dyld_cache_format.h)
  and [cache lookup](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/common/DyldSharedCache.cpp).
- [Mach-O structures](https://github.com/apple-oss-distributions/cctools/blob/e0d56624eca2a76c2ace4c21850df9e666de4ca5/include/mach-o/loader.h).
- [Fat wrapper structures](https://github.com/apple-oss-distributions/cctools/blob/e0d56624eca2a76c2ace4c21850df9e666de4ca5/include/mach-o/fat.h).
- [Loader path anchors and rpath order](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/dyld/Loader.cpp).
- [Linked dylib attributes and implicit edges](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/mach_o/UnsafeHeader.cpp).
- [Lazy-load format](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/mach_o/LazyLoadDylib.h)
  and [reader](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/mach_o/LazyLoadDylib.cpp).
- [Weak missing-image behavior](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/dyld/JustInTimeLoader.cpp)
  and [prebuilt references](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/dyld/PrebuiltLoader.cpp).
- [Loaded-cache UUID API](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/include/mach-o/dyld_priv.h).

Run the pure Python fixtures with the selected SDK Python:

```sh
"$PYTHON" -m unittest discover -s tests -p test_darwin_runtime.py -v
```

They check the routing graph, alias and subcache relationships, null weak edges,
lazy blobs and expansion bounds, platform bounds, changed bytes, read races,
and the OS service boundary.
Synthetic fixtures do not qualify a native bundle or prove a compiler build.
Actual cache metadata checks remain distinct from seed and ordinary acceptance.
