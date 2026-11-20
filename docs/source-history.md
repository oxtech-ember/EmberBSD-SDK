# Source history of kept objects

Kept objects and tools record the OS, Ports and SDK commits that built them.
A later build reuses them with any current commit: a later commit, an earlier
one, a rewritten one or one of other history. Git writes each file that it
changes with a new modification time, so `build.sh -u` builds again what the
changed files need. The recorded commits must still be in the repositories:
the scoped refreshes compare their files with the current files.

The other reuse rules do not change: the clean-source admission, the
comparison of the compiled-input signature, the scoped refresh rules and the
artifact checks. Saved source pins still record provenance. The rules of the
LLVM runtime base OS are separate.

The tests use real Git histories only, not a target build.
