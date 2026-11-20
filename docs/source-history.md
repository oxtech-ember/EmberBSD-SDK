# Source history of kept objects

Kept objects and tools record the OS, Ports and SDK commits that built them.
A later build reuses them with a later commit, an earlier one, a rewritten one
or one of other history. Git writes each file that it changes with a new
modification time, so `build.sh -u` builds again what the changed files need.
The recorded commits must still be in the repositories: the scoped refreshes
compare their files with the current files.

Make does not see a removed file. An object of a removed source can stay, and
a removed source or header can have shadowed another file. So an OS commit
that is not a descendant of the recorded commit must not remove, or change the
type of, a file of the recorded commit other than a document (`*.md`, a manual
page or `share/doc`); the builder refuses such objects, and the build needs
new objects. A descendant
commit is accepted as before.

The other reuse rules do not change: the clean-source admission, the
comparison of the compiled-input signature, the scoped refresh rules and the
artifact checks. Saved source pins still record provenance. The LLVM runtime
base still needs a descendant OS commit.

The tests use real Git histories only, not a target build.
