# Initial SDK tools on Linux

Check for a compatible installed native prefix before creating another one.
Use the [component client](sdk-client.md) to inspect authenticated catalogs and
plans when its Python, OpenSSL and pkg_tools prerequisites are available.
Native packages must match the Linux host CPU, libc and runtime. A NetBSD target
repository cannot supply Linux executables. Catalog contents can change; use the
actual plan rather than assuming that a compiler is published.

The [first-install shell script](bootstrap-client.md) currently implements the
Darwin arm64 bootstrap. Its platform refusal does not mean that Linux source
builds require that archive. When no compatible binary bootstrap exists, use
the existing Ports initial native bootstrap below. The client's source fallback
uses an already prepared native toolset; it does not bootstrap its first compiler.
Network, signature and integrity failures remain errors.

## Acquire the initial native prefix

Use a writable local filesystem with executable files, symbolic links and locks,
and build paths without whitespace or shell metacharacters. Record the SDK and
Ports commits and initialize Ports' pinned `upstream/pkgsrc` submodule.
Read the owning Ports [initial Linux bootstrap](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/cross/native-toolset.md#initial-linux-bootstrap).
It defines seed selection, pkgsrc bootstrap fragments, package configuration and
the transition to the selected native prefix. Keep recipes and version pins there.

1. Explicitly acquire an official native Clang/LLVM seed and compatible host ABI
   development files through the distribution's authenticated package provider
   or a verified upstream source build. Select its actual executable paths.
   Keep this temporary bootstrap seed separate from the final Ports prefix.
2. Provide CPython 3.15 for the control scripts, building the official source if
   necessary. Check the modules needed by the selected producers before source
   preparation:

   ```sh
   /absolute/python/bin/python3.15 -c 'import ctypes, ssl, zlib, lzma, bz2'
   ```

   LLVM's xz archive needs `lzma`; installing the `xz` executable alone does not
   add that Python module. Supply the corresponding development libraries before
   building Python. The [environment description](../cloud-init.json) records the
   common and producer-specific module requirements.
3. Create one canonical Ports export with explicit Git and patch paths. Reuse
   the export across source owners through their `--pkgsrc-export` option:

   ```sh
   /absolute/python/bin/python3.15 -B /absolute/Ports/scripts/pkgsrc-shared-export.py \
       /absolute/pkgsrc-export --sdk-source /absolute/EmberBSD-SDK \
       --git /absolute/bin/git --patch /absolute/bin/patch
   ```

4. Bootstrap pkgsrc with the explicitly selected seed. Follow Ports' distinct
   `--bootstrap-mk-fragment` and installed package configuration inputs. Include
   the exported common defaults in the initial package configuration. Select
   archive tools, linker, assembler and readelf explicitly for nested builds.
   Build native Clang/LLVM from the common recipes in that same prefix, with the
   host backend and the target backends required by the task.
   Install and select the prefix's `mksh` before CMake, as the Ports guide
   specifies. Check the shell in a generated Unix Makefile. Rebuild an initial
   CMake that still selects the host `/bin/sh` before ordinary builds.
5. Follow the documented [gettext composition](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/gettext-bootstrap.md):
   plain gettext tools, then GLib/libcroco, then replacement with the full style
   variant. Keep object directories separate. After CMake becomes available,
   replace the transitional `EMBERBSD_NATIVE_BOOTSTRAP` lz4 package with its
   ordinary full package as the native bootstrap guide requires.
6. Complete the task's native roles from common Ports recipes, including fetch
   and its OpenSSL CA package. Select `PKG_OPTIONS.libfetch+=openssl` when
   acquiring fetch through the initial bootstrap configuration.
   Generate the SDK native-tool include with Python
   from that prefix and configure the normal Ports native profile. Ordinary
   builds then use tools and compiler components from the selected prefix.

The native build runners discard ambient proxy settings. On a host that requires
a proxy, fetch needed archives through its declared network route before the
closed build. Keep the recipe's checksum checks and retain the shared distfile cache.

An OS build with `--gettext-source` can need native `sysutils/checkperms` and the
common Libtool source archive. Build missing native dependencies through common Ports.
The [cross-libtool producer](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/cross/prepare-libtool.py)
accepts `--distdir`. The OS build receipt records its complete command. After
fetching the archives, add `--distdir /absolute/existing/cache` to that command
and run it. The OS builder then reuses the completed producer context.
The OS release CLI also accepts `--distdir /absolute/existing/cache` and forwards
it to each cross-libtool invocation, including later context refreshes.

After a failed OS run, select a new empty destination and retain compatible
object, tool and runtime directories. See [OS release orchestration](os-release.md).

Retain verified source archives, installed packages and compatible build objects.
Do not delete them and rebuild solely because a binary host repository is absent.
Do not mark a partially bootstrapped prefix as a verified native toolset.

## Return to the component client

Configure the actual Linux host identity, prefix and pkgdb through the
[client configuration](sdk-client.md#use-the-client). Inspect the catalog and
task plan before installation. For example, with those prerequisites prepared:

```sh
/absolute/host-pkg-llvm/bin/python3.15 /absolute/EmberBSD-SDK/scripts/packages.py \
    --config /absolute/client.json list
/absolute/host-pkg-llvm/bin/python3.15 /absolute/EmberBSD-SDK/scripts/packages.py \
    --config /absolute/client.json plan --set kernel-tools
```

Set the documented `source` inputs for missing packages. Reuse the native prefix
and package cache across targets. A kernel needs no application sysroot;
applications additionally need a target/ABI sysroot and runtime.

These are the existing source and client interfaces. Passing source-export or
instruction tests alone does not establish a complete Linux compiler bootstrap,
an OS build, a booted kernel or hardware acceptance. Record those results from
their actual producers separately.
