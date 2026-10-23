# Libtool configuration text

`libtool.LIBTOOL_CONFIG_API = 1` provides `parse_libtool_config(text, tag)`.
GNU Libtool 2.6.2 `--tag=TAG --config` prints unmarked default configuration,
then the selected tag block. `CC` uses defaults without a tag block.
The parser returns `tag`, `defaults`, `overrides` and `effective` maps for
`host`, `host_os`, `CC` and `build_libtool_libs`. It requires unique scalar
assignments within each scope. A selected non-CC block must define `CC`.
Common fields inherit from defaults unless that block explicitly overrides them.

The parser rejects missing, duplicate, foreign, malformed or incomplete tag
markers and nonempty trailing output. It does not execute configuration text.
It rejects command-list syntax in the selected fields. Other Libtool fields
remain outside this scalar API. Consumers must retain bounded stdout, stderr
and command status. Consumers validate target, compiler, shared-library policy
and source identity. Parsed text does not qualify an executable or package.
