# Initial native package tools

The `pkg-install-bootstrap` producer stage builds the canonical libarchive and
`pkg_install` sources with the selected GCC 16 driver and native ABI. It uses
the fixed Ports `pkg-install.json` contract and the canonical pkgsrc bootstrap
sequence. It does not install a complete SDK or qualify an ordinary provider.

The plan uses the existing producer envelope and input acceptance. Its `program`
object contains `source`, `owner`, `manifest`, `compat`, `export`, and `profile`.
The first two use the existing program tree proof and complete inventories.
The source tree contains the complete `archivers/libarchive/` and
`pkgtools/pkg_install/` subtrees plus `bootstrap/bootstrap` and their ancestors.
Its bytes must match the pinned canonical export. The owner tree contains only
the fixed manifest, `pkgsrc/export.conf`, and the dyld-cache infrastructure patch.
The export and profile retain the existing metadata source chain. The `compat`
object retains the accepted initial libnbcompat headers, static library, actual
component commands, independent review, and historical compiler and ABI pins.

The stage copies source files with their recorded mtimes into fresh owned work.
It runs two configure/build pairs. Libarchive is static and has no programs,
compression libraries, or crypto providers. The package tools use
`--enable-bootstrap` and that libarchive plus the retained libnbcompat. This mode
excludes `pkg_delete` and normal signature and vulnerability facilities. The
ordinary common recipe retains its complete dependencies and security behavior.

The selected bmake tuple remains in direct and recursive invocations. A data
make fragment carries the actual generated `libarchive.pc` private libraries to
submakes. It permits library names only. It does not accept extra shell code or
search paths. Every configure and build command holds the existing heavy-link
lease. Builds can use the accepted CPU budget; links remain one at a time.
Cases run serially. The complete observation budget is at most 1200 seconds.

Sixteen fixed cases check all four versions, package patterns, uncompressed
package creation, real installation and registration, payload checks, a missing
required library, and refusal to count the active host cache for a separate
`pkg_add -P` target. Negative cases require the exact library diagnostic. Four
additional `-V` calls retain raw dyld observations. The selected-only PATH and
fixed `-F none` inputs do not admit generic compressed-package support or an
external decompressor. The full initial profile also sets `PKG_COMPRESSION=none`.

The existing runner retains work and input leases, process groups, drainage,
HOLD, input revalidation, and pending independent review. Output runtime
admission and provisioning remain separate. No result from this stage issues
ordinary SDK qualification.
