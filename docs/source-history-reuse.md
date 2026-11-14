# Reusing objects after history-only changes

Object-context history admission accepts an ancestor source pin, as before.
It also accepts a non-ancestor pin if Git proves that the old and current
commits have exactly the same complete tree. This covers an amended commit,
rebase or cherry-pick whose final tracked content is identical, without
requiring a clean rebuild solely because the commit identity changed.

This is not general semantic equivalence: a changed tracked file, executable
bit, symlink or submodule pin changes the tree. Missing old commits and Git
errors remain failures. Existing clean-source admission, compiled-input
signature comparison, scoped-refresh rules and artifact validation still
apply. Different source content is not admitted through this fallback.

Saved source pins continue to record provenance. No signed catalog format,
package name, dependency closure, binary reuse decision or security check is
changed. In particular this does not make packages with different revisions
interchangeable, or fix S3 misses caused by an accounting-only revision bump.

The change is scoped to object-context history admission. It does not relax
separate runtime/base-OS compatibility rules or establish runtime compatibility.
The focused tests exercise real Git histories only, not a target build.
