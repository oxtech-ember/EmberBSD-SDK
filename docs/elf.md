# Read ELF metadata

`emberbsd_sdk.elf` uses Python and its standard library. It reads regular files
and verifies their identity after the read. It does not invoke a compiler,
loader, `readelf`, or extractor.

`read_elf_header` supports the documented ELF32/ELF64 header scope.
`read_elf_dynamic(path, *, machine=None)` retains its existing ELF64 result:
header identity, `soname`, `needed`, `rpath`, `runpath`, and `relro`.
Header-only and relocatable-object inspection do not qualify dynamic linkage.

## Read dynamic relocations

Check `elf.ELF_RELOCATIONS_API == 1`, then call
`elf.read_elf_relocations(path, *, machine=None)`. This separate capability
does not change the library API version or the `read_elf_dynamic` result.

The input must be ELF64 little-endian, of type `ET_EXEC` or `ET_DYN`.
`machine`, when supplied, must match `e_machine`. The reader uses the shared
ELF header, program-header and dynamic-tag parser. It reads `DT_REL`, `DT_RELA`
and `DT_JMPREL` through `PT_DYNAMIC` and file-backed `PT_LOAD` ranges.
It neither reads nor requires section headers. Stripped and sectionless
files use the same path. An object with no `PT_DYNAMIC` has no advertised
dynamic relocation tables and returns an empty scan.

The result contains:

- `class`, `endian`, `machine`, `type`: the shared ELF header identity.
- `count`: the total number of relocation entries.
- `types`: sorted distinct integer types, the low 32 bits of ELF64 `r_info`.
- `type_counts`: sorted rows `{"type": integer, "count": integer}`.
- `tables`: rows in `REL`, `RELA`, `JMPREL` order, when advertised.
- `tlsdesc`: `{"plt": integer-or-null, "got": integer-or-null}`. These are
  the raw values of GNU `DT_TLSDESC_PLT` and `DT_TLSDESC_GOT`. Zero remains zero;
  `null` means absent. Their presence does not assert target support.

Each table contains `kind` (`REL`, `RELA`, or `JMPREL`), `encoding` (`REL` or
`RELA`), `address`, `file_offset`, `size`, `entry_size`, `count`, `types` and
`type_counts`. The address and file offset identify its exact file-backed
range. `REL` entries are 16 bytes. `RELA` entries are 24 bytes.
The corresponding address must be 8-byte aligned. A zero-size advertised
table still needs a unique file-backed address and valid paired tags.

PLT-only files need the complete `DT_JMPREL`/`DT_PLTRELSZ`/`DT_PLTREL` tuple.
`DT_PLTREL` determines their entry format. An optional `DT_RELENT` or
`DT_RELAENT` is not required for this case. If present, its value must be
correct. Main `REL` and `RELA` tables require their pointer, size and entry-size
tags together. Orphan entry-size tags are refused. GNU `DT_RELCOUNT` and
`DT_RELACOUNT` must not exceed the corresponding main table's entry count.

The reader refuses ambiguous mappings, truncated or overflowing ranges,
duplicate relocation singleton tags, incomplete tuples, wrong sizes and
nonempty overlaps between tables or with the ELF header, program-header
array or dynamic segment. Adjacent half-open ranges are valid.
Each table must fit in one unique file-backed load segment. Memory-only BSS
does not supply relocation bytes. The combined limit is 1,000,000 entries;
existing program-header and dynamic-segment limits also apply.

Known packed formats are refused explicitly, even when their size is zero:
gABI `DT_RELR`/`DT_RELRSZ`/`DT_RELRENT`, Android APS2 REL/RELA tags, and the
legacy Android RELR tags. An error raises `SDKError` and returns no partial
scan. The API does not decode static section relocations, symbols, addend
semantics, or relocation destination memory. It does not implement a linker.
Target type support and package acceptance belong to the caller.

The values and entry layouts follow the [ELF gABI](https://gabi.xinuos.com/elf/08-dynamic.html),
[GNU dynamic extensions](https://sourceware.org/git/?p=glibc.git;a=blob;f=elf/elf.h),
and [Android encoding definitions](https://android.googlesource.com/platform/bionic/+/361ba86734fb2821a6adcfdf775db8abd04e0de0/libc/include/elf.h).
Machine-specific relocation names belong to the target ABI. For example,
`R_X86_64_TLSDESC` is numeric type 36; the SDK reports it without deciding
whether a target loader supports it.

Run the hand-authored byte fixtures and existing core contracts with CPython
3.15. They need no target libraries, compiler, or generated ELF files:

```sh
python3.15 -B -I -S -m unittest discover -s tests -p 'test_elf_relocations.py'
python3.15 -B -I -S -m unittest discover -s tests -p 'test_core.py'
```
