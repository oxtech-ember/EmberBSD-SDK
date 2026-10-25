# NetBSD first-native GCC16 recipe route

The first-native recipe controller has a fixed NetBSD 11 amd64 branch in
`bootstrap-seed.py run-bootstrap`. It uses the existing
`emberbsd-native-gcc16-bootstrap-plan` envelope and the existing independent
graduation flow. It does not issue acceptance or qualify an ordinary
`NativeToolset`.

The plan pins a source-export acceptance and target transfer acceptance. The
transfer receipt is authoritative on the target; validation does not reopen a
foreign exporter checkout. Live validation checks the complete transferred
tree, profile, MAKECONF, utility members, accepted pkgsrc tools, and compiler
seed. Historical graduation checks receipt lineage and retained inventory
bytes without inspecting retired source trees.

`netbsd_profile` is exactly
`netbsd11-amd64-pkgsrc-native-gcc16-v1`. The `netbsd` object contains exactly:

| Field | Binding |
| --- | --- |
| `pkgsrc_tools_acceptance` | Independently pinned `pkgsrc-bootstrap-tools` receipt; it selects only bmake, gmake and mksh. |
| `source_export_acceptance`, `source_transfer_acceptance`, `source_inventory` | Exact current Ports export, target-side full inventory and transfer evidence. |
| `ports_export_root`, `ports_source` | Same root and revision as the common plan's Ports source pin. |
| `profile` | File reference for `<ports_export_root>/EMBERBSD-NETBSD-BOOTSTRAP-MK.CONF`, whose bytes match the transferred inventory. Its source is Ports `profiles/development-toolchain/netbsd-bootstrap.mk.conf`. |
| `host_facts` | `{os: NetBSD, cpu: x86_64, kernel_version: 11.0, pkgsrc_opsys_version: 110000}` on the current target. The SDK derives the last number from the kernel release. |
| `utility_bin`, `utility_tools` | A dedicated directory whose members are exactly the lowercase helper roles in the roster. Each is a pinned regular executable. Compiler, assembler/linker/archive, shell and make roles, undeclared files and symlinks are refused. MAKECONF exposes each as `EMBERBSD_NETBSD_TOOL.<role>` and lists the sorted names in `EMBERBSD_NETBSD_NATIVE_TOOLS`. |
| `makeconf` | Exact file reference. Its contents are rendered by `render_netbsd_makeconf()` and end with the include of the pinned Ports profile. |
| `pkgsrc_tools`, `pkgsrc_mk` | Exact bmake/gmake/mksh paths and `share/mk` tree selected from the independently accepted pkgsrc-bootstrap-tools prefix inventory. |
| `binutils_as`, `binutils_ld` | Exactly `output_bundle_root/bin/gas` and `output_bundle_root/bin/gld`. |

The fixed MAKECONF binds `EMBERBSD_NETBSD_HOST_OS/CPU`, kernel and pkgsrc
release facts, scoped shell/make routes and utility mappings, the source-seed
GCC command tuples, the initial seed AS/LD/AR/RANLIB/NM/STRIP paths, and private
pkgsrc directories. Profile-only facts use the `EMBERBSD_NETBSD_*` and
`EMBERBSD_GCC16_*` namespaces; generic pkgsrc selectors such as `MAKE_JOBS`,
`PKGSRC_COMPILER`, `GCC_REQD`, `GCCBASE`, `LOCALBASE`, `PREFIX`, `WRKOBJDIR`,
`DISTDIR`, `PKG_DBDIR`, and `PACKAGES` retain their standard names. It sets
`EMBERBSD_NETBSD_JOBS=1`. Package bmake commands
use `-m` with the accepted pkgsrc prefix's `share/mk`; they do not pass a
top-level `-j` option. The fixed profile selector
`EMBERBSD_NETBSD_BOOTSTRAP=yes` is present across the full recipe closure.
Ports owns the host/tool routing and confines GCC-specific behavior to
`PKGPATH == lang/gcc16`.

The recipe DAG must contain `devel/binutils` before `lang/gcc16`. Only the GCC
build and install steps select the source-built `LOCALBASE/bin/gas` and
`LOCALBASE/bin/gld`. The plan keeps a real serial `gmake compare` phase; a
failed comparison remains a failed build result.

## Admission limit

The available compiler record is an `emberbsd-bootstrap-compiler-seed` with
scope `bmake-bootstrap`. It remains valid only for its recorded bootstrap
purpose. It cannot be renamed to `gcc16-bootstrap`, used as an ordinary
provider, or loaded as a complete `emberbsd-bootstrap-seed`. The NetBSD recipe
controller therefore requires a separately accepted full GCC16 seed descriptor
before it can run. A future NetBSD first-native admission must preserve the old
seed provenance and explicitly bind any transient OS assembler/linker inputs;
the final ordinary output must still use this plan's source-built binutils.

The runner records a pending-review result with drained commands and output
inventory. A separate reviewer must inspect the seed admission, source and
transfer receipts, fixed profile, command logs and exits, input revalidation,
and complete output. Only an independent graduation and NativeToolset receipt
can authorize ordinary native builds.
