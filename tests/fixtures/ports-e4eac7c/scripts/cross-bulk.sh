#!/bin/sh
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
# Build a list of pkgsrc packages in dependency order with the cross MAKECONF.
#
#   order  Find all dependencies of the list and sort them. Writes STATE/order.
#          Independent frontier queries use CROSS_BULK_JOBS (default: CPUs).
#          List entries the profile skips (PKG_SKIP_REASON) go to
#          STATE/skipped. A graph in which a package reaches a skipped
#          package is refused: no order is written.
#   fetch  Get the distfiles for STATE/order. Only network and DISTDIR.
#          A package whose distinfo distfiles are all in DISTDIR with
#          their recorded sizes is not queried; the build verifies the
#          checksums.
#   build  Build and install each node of STATE/order, several at once
#          (CROSS_BULK_JOBS, see build below).
#          Independent work drains after a failure. Exit 0 means the pass
#          has no failed or blocked packages; exit 1 means it does.
#
# The graph has two kinds of nodes. PKGPATH is a target package: its
# DEPENDS and BUILD_DEPENDS are target nodes, its TOOL_DEPENDS and
# BOOTSTRAP_DEPENDS are native nodes PKGPATH@native, built for the native
# host prefix (USE_CROSS_COMPILE=no). Every dependency of a native node is
# native. So the build installs each native tool once, as its own job,
# before the packages that need it, and pkgsrc's depends step finds it.
#
# Fetch and build check PKG_SKIP_REASON again before they act on a package:
# an order written by an older tree or by hand may name a package that the
# profile now skips. Such a package goes to STATE/skipped, never to a
# failure list, and the packages that depend on it are blocked.
#
# The build options of target dependencies that are not installed come
# from the tree (mk/pkg-build-options.mk). Order keeps a pkgsrc
# PBULK_CACHE_DIRECTORY in STATE/build-options, as a pbulk scan does.
# A repeat order reuses it only while the tree, MAKECONF, native selection,
# bmake environment, installed package metadata and prior options are
# unchanged. Fetch uses the same gate. Build does not use it: installed
# dependencies answer there.
#
# Each pass checks every done package by its PKGNAME, and a target-mode
# parse costs seconds. Order and build keep show-vars answers in STATE/vars
# with the makefiles that parse read (.MAKE.MAKEFILES) and the device, inode,
# size and modification time of each of them and their parent directories.
# Order and build also bind answers to their make/configuration context. A
# changed makefile or a file added next to one invalidates its answer.
# CROSS_BULK_VARS_CACHE=no turns the cache off.
#
# Before a package builds, build renames its old WRKDIR into a trash
# directory on the same file system (WRKOBJDIR/.cross-bulk-trash), so the
# WRKDIR path is free at once and pkgsrc's clean step has nothing left to
# remove. One deleter per build removes the trash in the background, one
# tree at a time and at the lowest priority, and stops with the build; the
# next build removes what a stopped or crashed run left there. Without a
# WRKOBJDIR, or for a WRKDIR on another file system, the clean step removes
# the WRKDIR as before (CROSS_BULK_TRASH=no forces that).
#
# A built package is recorded in STATE/done with its PKGNAME and SHA256.
# The build skips a package whose PKGPATH and PKGNAME have a record there
# and whose package file exists; the file is not hashed again. A new
# PKGNAME or a missing package file builds it again.
# Failures go to STATE/failed. Packages with a failed dependency go to
# STATE/blocked. Logs are in STATE/logs.
set -eu
umask 022

[ "$#" -eq 3 ] || {
    echo 'Usage: scripts/cross-bulk.sh order|fetch|build|selected-order|selected-build PKGPATH_LIST ABSOLUTE_STATE' >&2
    echo '       scripts/cross-bulk.sh package|package-install|patch-refresh|buildlink-refresh PKGPATH[@native] ABSOLUTE_STATE' >&2
    exit 2
}
mode=$1
list=$2
state=$3
case "$state" in
    /*) ;;
    *) echo "Not an absolute path: $state" >&2; exit 2 ;;
esac
: "${BMAKE:?Set BMAKE to the host bmake}"
: "${MAKECONF:?Set MAKECONF to the cross MAKECONF}"
: "${PKGSRCDIR:?Set PKGSRCDIR to the prepared pkgsrc tree}"
# Order sorts its graph with tsort. Without tsort every graph would be
# refused as cyclic after all queries, so refuse before the first one.
case "$mode" in
    order|selected-order|selected-build)
        command -v tsort > /dev/null 2>&1 || {
            echo 'The selected PATH must provide tsort for the dependency order' >&2
            exit 2
        } ;;
esac
export MAKECONF
# Target and native compiler roles are selected by MAKECONF and the SDK
# mapping. An inherited host CPP (or binutils role) must not leak into an
# Autoconf process after bmake resolves a different target tool.
unset CC CXX CPP AR AS LD NM RANLIB STRIP CLANGBASE
controller_shell=${CROSS_BULK_SHELL:-sh}
if [ -n "${CROSS_BULK_SHELL:-}" ]; then
    case "$controller_shell" in /*) ;; *) echo 'CROSS_BULK_SHELL must be an absolute selected shell path' >&2; exit 2 ;; esac
    [ -x "$controller_shell" ] || { echo 'CROSS_BULK_SHELL is not executable' >&2; exit 2; }
fi
state_lock_owned=no
state_lock_release_safe=yes
stage_supervised=no
if [ -n "${CROSS_BULK_SUPERVISED:-}" ]; then
    [ "$CROSS_BULK_SUPERVISED" = "$PPID|$mode|$list|$state" ] || {
        echo 'Invalid supervised cross-bulk context' >&2; exit 2;
    }
    stage_supervised=yes
    unset CROSS_BULK_SUPERVISED
fi
state_lock_acquire()
{
    mkdir -p "$state"
    if ! mkdir "$state/build.lock" 2> /dev/null; then
        other=$(cat "$state/build.lock/pid" 2> /dev/null || :)
        echo "Another build${other:+ ($other)} uses $state" >&2
        return 2
    fi
    state_lock_owned=yes
    if ! printf '%s\n' "$$" > "$state/build.lock/pid"; then
        rmdir "$state/build.lock" 2> /dev/null || :
        state_lock_owned=no
        return 1
    fi
}
state_lock_release()
{
    [ "$state_lock_owned" = yes ] || return 0
    rm -f "$state/build.lock/pid"
    rmdir "$state/build.lock" 2> /dev/null || :
    state_lock_owned=no
}
case "$mode" in
    order|fetch|build|package|package-install|patch-refresh|buildlink-refresh|selected-order|selected-build)
        if [ "$stage_supervised" = no ]; then state_lock_acquire || exit $?; fi
        trap 'if [ "$state_lock_release_safe" = yes ]; then state_lock_release; else echo "Workers may not have drained; keeping lock $state/build.lock" >&2; fi' EXIT
        ;;
esac
mkdir -p "$state/logs"

# The selected native prefix may provide the BSD awk implementation as nawk.
# Use it only when the curated PATH has no awk command; never fall through to
# an unselected host copy.
if ! command -v awk > /dev/null 2>&1; then
    if command -v nawk > /dev/null 2>&1; then
        awk() { nawk "$@"; }
    else
        echo 'The selected native prefix must provide awk or nawk' >&2
        exit 2
    fi
fi

# Hash the complete NUL-delimited environment without writing its values to
# disk. If a selected env/sort implementation cannot provide a trustworthy
# digest, the order still runs but gets no persistent query/options cache.
environment_identity()
{
    env_status=$state/environment.$$.env-status
    sort_status=$state/environment.$$.sort-status
    digest_file=$state/environment.$$.digest
    rm -f "$env_status" "$sort_status" "$digest_file"
    if (env -0 || { status=$?; printf '%s\n' "$status" > "$env_status"; exit "$status"; }) |
        (LC_ALL=C sort -z || { status=$?; printf '%s\n' "$status" > "$sort_status"; exit "$status"; }) |
        shasum -a 256 > "$digest_file"; then
        status=0
    else
        status=$?
    fi
    if [ "$status" -ne 0 ] || [ -e "$env_status" ] || [ -e "$sort_status" ] ||
        [ ! -s "$digest_file" ]; then
        rm -f "$env_status" "$sort_status" "$digest_file"
        return 1
    fi
    digest=$(awk 'NR == 1 { print $1 }' "$digest_file")
    rm -f "$env_status" "$sort_status" "$digest_file"
    case "$digest" in ''|*[!0-9a-f]*) return 1 ;; esac
    [ "${#digest}" -eq 64 ] || return 1
    printf '%s\n' "$digest"
}

# Verify the selected native prefix once per run. Every later bmake call,
# and its recursive queries, receive the verified selection on the command
# line and skip the per-parse verifier.
# Parallel order queries run as child runs of this script (mode deps) and
# get the verified selection in CROSS_BULK_NATIVE.
unset EMBERBSD_NATIVE_TOOLSET_VERIFIED
case "$mode" in
    order|fetch|selected-order|selected-build)
        unset CROSS_BULK_ENV_IDENTITY
        env_identity=$(environment_identity) || env_identity= ;;
    deps) env_identity=${CROSS_BULK_ENV_IDENTITY:-} ;;
    *) env_identity= ;;
esac
export CROSS_BULK_ENV_IDENTITY=$env_identity
if [ "$mode" = deps ] && [ -n "${CROSS_BULK_NATIVE:-}" ]; then
    native=$CROSS_BULK_NATIVE
else
    native=$("$BMAKE" -C "$PKGSRCDIR/pkgtools/pkg_install" show-var \
        VARNAME=EMBERBSD_NATIVE_TOOLSET_VERIFIED < /dev/null) || {
        echo 'The native host prefix selection failed verification' >&2
        exit 2
    }
fi
unset CROSS_BULK_NATIVE
[ -n "$native" ] || { echo 'The MAKECONF has no native host prefix selection' >&2; exit 2; }

cache=$state/build-options
cachearg=
vars_cache=
vars_context=
build_vars_context=
gnu_stat=no
case "$mode" in
    order|fetch|selected-order|selected-build) ! stat -c %i / > /dev/null 2>&1 || gnu_stat=yes ;;
    deps) [ -z "${CROSS_BULK_VARS_CONTEXT:-}" ] ||
        ! stat -c %i / > /dev/null 2>&1 || gnu_stat=yes ;;
esac
if [ "$mode" = deps ] && [ -n "${CROSS_BULK_OPTIONS_CACHE:-}" ]; then
    expected_cache=$(dirname "$state")/build-options
    [ "$CROSS_BULK_OPTIONS_CACHE" = "$expected_cache" ] || {
        echo 'The order query cache is outside its owning state directory' >&2
        exit 2
    }
    cache=$CROSS_BULK_OPTIONS_CACHE
    cachearg=$cache
fi

mk()
{
    mk_lock_was_safe=$state_lock_release_safe
    if [ "$state_lock_owned" = yes ]; then state_lock_release_safe=no; fi
    if "$BMAKE" EMBERBSD_NATIVE_TOOLSET_VERIFIED="$native" \
        ${cachearg:+"PBULK_CACHE_DIRECTORY=$cachearg"} "$@"; then
        mk_status=0
    else
        mk_status=$?
    fi
    if [ "$state_lock_owned" = yes ] && [ "$mk_status" -lt 128 ]; then
        state_lock_release_safe=$mk_lock_was_safe
    fi
    return "$mk_status"
}

# pkg-build-options.mk keys cache files by PKGBASE. One graph can contain a
# target package and its native build tool with the same base but different
# installed options, so keep the existing node role in the cache directory.
option_cache_role()
{
    [ -n "$cachearg" ] || return 0
    case "$1" in
        *@native) cachearg=$cache/native ;;
        *) cachearg=$cache/target ;;
    esac
}

# Inputs for cached dependency queries and pkg-build-options results: native
# selection, tree identity, selected make tool, environment, pkg_install
# makefiles and the target/native installed package metadata read for PKG_OPTIONS.
cache_identity()
{
    [ -n "$env_identity" ] || return 1
    files=$(var pkgtools/pkg_install .MAKE.MAKEFILES) || return 1
    printf '%s\n' "PKGSRCDIR=$PKGSRCDIR" "BMAKE=$BMAKE" "MAKECONF=$MAKECONF" \
        "native=$native" "environment-sha256=$env_identity"
    ls -id "$PKGSRCDIR/" | awk '{ print $1 }'
    for file in $files; do
        case "$file" in /*) shasum -a 256 "$file" || return 1 ;; esac
    done
    pkgdb_snapshot
}

pkgdb_snapshot_one()
{
    db=$1 role=$2
    printf 'pkgdb=%s %s\n' "$role" "$db"
    [ -d "$db" ] || { printf 'missing\n'; return 0; }
    db=$(CDPATH= cd -- "$db" && pwd -P) || return 1
    if [ "$gnu_stat" = yes ]; then
        snapshot=$(find "$db" \( -type d -o -type f \( -name '+BUILD_INFO' -o -name pkgdb.byfile.db \) \) \
            -exec stat -c '%d %i %s %.9Y %.9Z %n' {} +) || return 1
    else
        snapshot=$(find "$db" \( -type d -o -type f \( -name '+BUILD_INFO' -o -name pkgdb.byfile.db \) \) \
            -exec stat -f '%d %i %z %Fm %Fc %N' {} +) || return 1
    fi
    printf '%s\n' "$snapshot" | sort
}

pkgdb_snapshot()
{
    target_pkgdb=$(mk -C "$PKGSRCDIR/pkgtools/pkg_install" show-var VARNAME=_PKG_DBDIR < /dev/null) || return 1
    native_pkgdb=$(mk -C "$PKGSRCDIR/pkgtools/pkg_install" USE_CROSS_COMPILE=no \
        show-var VARNAME=_HOST_PKG_DBDIR < /dev/null) || return 1
    case "$target_pkgdb:$native_pkgdb" in /*:/*) ;; *) return 1 ;; esac
    pkgdb_snapshot_one "$target_pkgdb" target || return 1
    pkgdb_snapshot_one "$native_pkgdb" native || return 1
}

options_snapshot()
{
    for role in target native; do
        dir=$cache/$role
        printf 'options=%s\n' "$role"
        [ -d "$dir" ] || { printf 'missing\n'; continue; }
        if [ "$gnu_stat" = yes ]; then
            snapshot=$(find "$dir" \( -type d -o -type f -name 'build-options.*' \) \
                -exec stat -c '%d %i %s %.9Y %.9Z %n' {} +) || return 1
        else
            snapshot=$(find "$dir" \( -type d -o -type f -name 'build-options.*' \) \
                -exec stat -f '%d %i %z %Fm %Fc %N' {} +) || return 1
        fi
        printf '%s\n' "$snapshot" | sort
    done
}

cache_matches_current()
{
    cache_identity > "$state/identity" || { rm -f "$state/identity"; return 1; }
    [ -f "$cache/identity" ] && cmp -s "$state/identity" "$cache/identity" || return 1
    [ -f "$cache/options.identity" ] && options_snapshot > "$state/options.identity" &&
        cmp -s "$state/options.identity" "$cache/options.identity" || return 1
    [ "$mode" != order ] || cmp -s "$list" "$cache/order-roots" || return 1
    set -f
    set -- $(cat "$state/order" "$list" 2>/dev/null | sed 's/@native$//' | sort -u)
    set +f
    [ -z "$(cd "$PKGSRCDIR" && find mk "$@" -newer "$cache/identity" -print | head -n 1)" ]
}

new_cache()
{
    cachearg=
    if cache_matches_current; then
        cachearg=$cache
        rm -f "$state/identity" "$state/options.identity"
        return 0
    fi
    rm -rf "$cache"
    rm -f "$state/options.identity"
    mkdir "$cache"
    if [ -s "$state/identity" ]; then
        mv "$state/identity" "$cache/identity"
    else
        rm -f "$state/identity"
        cache_identity > "$cache/identity" || { rm -rf "$cache"; return 0; }
    fi
    rm -f "$state/options.identity"
    cat "$list" > "$cache/order-roots"
    cachearg=$cache
}

# Use the order cache only with the same inputs, and if no file in mk/ or
# in an ordered package directory changed after the order run started.
reuse_cache()
{
    if cache_matches_current; then
        cachearg=$cache
    else
        echo 'The build options cache is not from this tree; querying without it' >&2
    fi
    rm -f "$state/identity" "$state/options.identity"
}

var()
{
    mk -C "$PKGSRCDIR/$1" show-var VARNAME="$2" < /dev/null
}

# The pkg_info of the native prefix, for the tools it already has.
native_info()
{
    [ -z "${native_pkg_info:-}" ] || return 0
    native_pkg_info=
    [ "$mode" != deps ] || native_pkg_info=${CROSS_BULK_PKG_INFO:-}
    [ -n "$native_pkg_info" ] ||
        native_pkg_info=$(mk -C "$PKGSRCDIR/pkgtools/pkg_install" USE_CROSS_COMPILE=no \
            show-var VARNAME=PKG_INFO < /dev/null)
    [ -n "$native_pkg_info" ] || { echo 'The native PKG_INFO is empty' >&2; exit 2; }
}

# Whether the native prefix has the package base $1 at the version of the
# PKGNAME $2 or newer. pkg_info matches BASE>=VERSION with the version
# order that pkgsrc uses for dependency patterns. A newer installed package
# is current; an older one is not, and its node is built again.
native_current()
{
    [ -n "$1" ] || return 1
    case "$2" in
        "$1"-[0-9]*) set -- "$1>=${2#"$1"-}" ;;
        *) set -- "$1" ;;
    esac
    set -f
    if $native_pkg_info -e "$1" > /dev/null 2>&1; then set +f; return 0; fi
    set +f
    return 1
}

# Whether the native prefix has a package that matches the dependency
# pattern $1.
native_satisfies()
{
    set -f
    if $native_pkg_info -e "$1" > /dev/null 2>&1; then set +f; return 0; fi
    set +f
    return 1
}

# Print the PKG_SKIP_REASON of a node on the first line, then its
# dependency nodes, one per line. A dependency of a native node that the
# native prefix already satisfies is followed by the word "soft": sort_edges
# may drop its edge to break a cycle. A native node that the native prefix
# already has at its version or newer is not built again, so its own build
# tools do not matter. This is what breaks the cycles among the bootstrap
# tools (coreutils and gettext-tools need each other, python needs both).
# Its runtime dependencies (DEPENDS) remain its dependency nodes: its
# consumers run it, and it runs them. An installed meson runs ninja, so a
# ninja that this build updates is installed before meson's consumers start.
node_deps()
{
    case "$1" in
        *@native)
            node=$1
            if [ -n "${CROSS_BULK_CROSSFED:-}" ] && grep -qFx "$node" "$CROSS_BULK_CROSSFED"; then
                # Match build_native: target-fed native tools need the target identity.
                set -f
                set -- "${node%@native}" @native USE_CROSS_COMPILE=no $CROSS_BULK_TARGET_SETTINGS
                set +f
            else
                set -- "${node%@native}" @native USE_CROSS_COMPILE=no
            fi ;;
        *) set -- "$1" '' ;;
    esac
    dir=$1 suffix=$2
    shift 2
    option_cache_role "$suffix"
    # The dependencies come from the makefiles alone. A WRKDIR left by an
    # earlier build has the saved MAKEVARS of its phases
    # (WRKDIR/.PHASE_makevars.mk), and a parse includes the latest of them
    # unless _MAKEVARS_MK is defined: a USE_BUILTIN.openssl=yes saved before
    # security/heimdal chose PREFER.openssl=pkgsrc dropped its openssl edge.
    vars=$(pkg_vars "$dir" 'PKG_SKIP_REASON DEPENDS BUILD_DEPENDS TOOL_DEPENDS BOOTSTRAP_DEPENDS PKGBASE PKGNAME' "$@") ||
        return 1
    printf '%s\n' "$vars" | sed -n 1p || return 1
    # Reuse this already evaluated identity for binary-first clients.
    name=$(printf '%s\n' "$vars" | sed -n 7p) || return 1
    printf '%s %s\n' "$dir$suffix" "$name" > "$state/$key.package"
    current=no
    if [ -n "$suffix" ]; then
        base=$(printf '%s\n' "$vars" | sed -n 6p) || return 1
        name=$(printf '%s\n' "$vars" | sed -n 7p) || return 1
        ! native_current "$base" "$name" || current=yes
    fi
    # pkgsrc resolves each dependency directory from the package that declares
    # it. show-depends-pkgpaths keeps that directory with :C/^[^:]*://, the
    # text after the first colon. A same-category sibling is as real as
    # ../../category/package. A path that is not one category/package inside
    # this tree is an error: omitting it would build the dependent with that
    # provider absent.
    deps=$(printf '%s\n' "$vars" | awk -v suffix="$suffix" -v current="$current" -v pkgdir="$dir" '
        function fail(msg) {
            print "cross-bulk: " msg > "/dev/stderr"
            exit 2
        }
        function resolve(base, rel,    n, m, i, count, part, raw, stack) {
            if (rel ~ /^\//) fail("absolute dependency path in " base ": " rel)
            # A trailing slash is an empty component. graphics/MesaLib/ is the
            # same declaring package as graphics/MesaLib; "." is not a step.
            n = split(base, raw, "/")
            count = 0
            for (i = 1; i <= n; i++) {
                if (raw[i] == "" || raw[i] == ".") continue
                count++
                stack[count] = raw[i]
            }
            m = split(rel, part, "/")
            for (i = 1; i <= m; i++) {
                if (part[i] == "" || part[i] == ".") continue
                if (part[i] == "..") {
                    if (count < 1) fail("dependency path leaves the pkgsrc tree: " rel " from " base)
                    count--
                    continue
                }
                if (part[i] ~ /[^A-Za-z0-9_.+-]/)
                    fail("unsupported dependency path in " base ": " rel)
                count++
                stack[count] = part[i]
            }
            if (count != 2 || stack[1] == "" || stack[2] == "")
                fail("dependency path is not a category/package: " rel " from " base)
            return stack[1] "/" stack[2]
        }
        NR == 1 || NR > 5 || (current == "yes" && NR != 2) { next }
        {
            s = NR <= 3 ? suffix : "@native"
            n = split($0, w, " ")
            for (i = 1; i <= n; i++) {
                if (w[i] == "") continue
                colon = index(w[i], ":")
                if (colon < 1) fail("dependency entry has no package path: " w[i] " from " pkgdir)
                pattern = substr(w[i], 1, colon - 1)
                rel = substr(w[i], colon + 1)
                if (pattern == "" || rel == "")
                    fail("malformed dependency entry: " w[i] " from " pkgdir)
                print resolve(pkgdir, rel) s, pattern
            }
        }') || return 1
    # A dependency is soft only when every pattern that names it is satisfied.
    printf '%s\n' "$deps" | while read -r dep pattern; do
        [ -n "$dep" ] || continue
        if [ -n "$suffix" ] && native_satisfies "$pattern"; then echo "$dep 1"; else echo "$dep 0"; fi
    done | awk '{ if (!($1 in soft)) soft[$1] = 1; if ($2 == 0) soft[$1] = 0 }
        END { for (d in soft) print d (soft[d] ? " soft" : "") }' | sort -u
}

# Print "dependent -> skipped" for each edge in $2 that reaches a node
# listed in $1. A skipped package is outside this profile; nothing may
# depend on it.
skipped_deps()
{
    awk 'FILENAME == ARGV[1] { skip[$1] = 1; next } ($1 in skip) && $1 != $2 { print $2 " -> " $1 }' "$1" "$2"
}

# Sort the edges $1 into $2. A cycle between native nodes to build, such as
# coreutils and perl after a revision bump of both, is broken at the first
# (sorted) edge of the cycle listed in the soft edges $3: the native prefix
# already satisfies its dependency, so pkgsrc builds the dependent with the
# installed package. The dropped edge is removed from $1 and reported. Any
# other cycle is refused: tsort would still print an order, with some
# package before its dependency.
sort_edges()
{
    while ! { tsort "$1" > "$2" 2> "$state/tsort.err" && [ ! -s "$state/tsort.err" ]; }; do
        cut=$(awk 'FILENAME == ARGV[1] { if ($1 == "tsort:" && NF == 2) c[$2] = 1; next }
            ($1 in c) && ($2 in c) && $1 != $2 { print $1, $2 }' \
            "$state/tsort.err" "${3:-/dev/null}" | sort -u | sed -n 1p)
        [ -n "$cut" ] || break
        echo "Cycle broken: ${cut#* } builds with the installed ${cut% *} (pattern satisfied)" >&2
        awk -v cut="$cut" '$1 " " $2 != cut' "$1" > "$1.cut" && mv "$1.cut" "$1" || return 1
    done
    [ -s "$state/tsort.err" ] || return 0
    echo 'Refused: the dependency graph has a cycle:' >&2
    cat "$state/tsort.err" >&2
    rm -f "$2"
    return 1
}

order()
{
    order_stop=0
    trap 'order_stop=1' HUP INT TERM
    native_info
    new_cache
    if [ -n "$cachearg" ] && [ "${CROSS_BULK_VARS_CACHE:-yes}" != no ]; then
        mkdir -p "$state/vars"
        vars_context="deps|$BMAKE|$MAKECONF|$PKGSRCDIR|$native|$env_identity|$cachearg|$(cksum < "$cache/identity" | awk '{ print $1 ":" $2 }')|$(sigs "$cache/identity")"
    else
        vars_context=
    fi
    jobs=${CROSS_BULK_JOBS:-$(host_cpus)}
    count CROSS_BULK_JOBS "$jobs"
    q=$state/order-query
    rm -rf "$q"
    mkdir "$q"
    : > "$q/failed"
    : > "$state/edges.new"
    : > "$state/soft.new"
    : > "$state/skipped.new"
    : > "$state/packages.new"
    sort -u "$list" > "$state/queue"
    : > "$state/seen"
    : > "$q/native-pending"
    : > "$q/crossfed"
    target_settings=
    order_phase=target
    query_index=0
    while [ -s "$state/queue" ] || { [ "$order_phase" = target ] && [ -s "$q/native-pending" ]; }; do
        [ "$order_stop" = 0 ] || { trap - HUP INT TERM; return 143; }
        if [ "$order_phase" = target ]; then
            # No native query runs until every target edge has been seen. A
            # native root encountered early can also be a later target tool.
            awk '/@native[ \t]*$/ { print }' "$state/queue" >> "$q/native-pending"
            awk '!/@native[ \t]*$/ { print }' "$state/queue" > "$state/target-queue"
            mv "$state/target-queue" "$state/queue"
            if [ ! -s "$state/queue" ]; then
                order_phase=native
                sort -u "$q/native-pending" > "$state/queue"
                awk '$1 ~ /@native$/ && $2 !~ /@native$/ { print $1 }' "$state/edges.new" | sort -u > "$q/crossfed"
                if [ -s "$q/crossfed" ]; then
                    target_settings=$(var pkgtools/pkg_install CROSSTARGETSETTINGS) || return 1
                fi
            fi
        fi
        : > "$state/next"
        : > "$q/frontier"
        : > "$q/frontier-map"
        awk 'function trim(s) {
                 sub(/^[ \t]+/, "", s)
                 sub(/[ \t]+$/, "", s)
                 return s
             }
             FILENAME == ARGV[1] { seen[trim($0)] = 1; next }
             { path = trim($0) }
             !(path in seen) { seen[path] = 1; print path }' \
            "$state/seen" "$state/queue" > "$state/queue.filtered"
        cat "$state/queue.filtered" >> "$state/seen"
        mv "$state/queue.filtered" "$state/queue"
        while read -r path; do
            key=node-$query_index
            query_index=$((query_index + 1))
            printf '%s|%s\n' "$key" "$path" >> "$q/frontier"
            printf '%s %s\n' "$path" "$key" >> "$q/frontier-map"
        done < "$state/queue"
        if [ -s "$q/frontier" ]; then
            [ "$order_stop" = 0 ] || { trap - HUP INT TERM; return 143; }
            state_lock_release_safe=no
            if CROSS_BULK_NATIVE=$native CROSS_BULK_PKG_INFO=$native_pkg_info \
                CROSS_BULK_OPTIONS_CACHE=$cachearg \
                CROSS_BULK_VARS_CONTEXT="$vars_context" \
                CROSS_BULK_CROSSFED="$q/crossfed" \
                CROSS_BULK_TARGET_SETTINGS="$target_settings" \
                xargs -P "$jobs" -I '{}' "$controller_shell" "$0" deps '{}' "$q" \
                < "$q/frontier"; then
                xargs_status=0
            else
                xargs_status=$?
            fi
            if [ "$xargs_status" -ge 128 ]; then
                echo "Dependency query workers exited with status $xargs_status; keeping the state lock" >&2
                return "$xargs_status"
            fi
            state_lock_release_safe=yes
            [ "$order_stop" = 0 ] || { trap - HUP INT TERM; return 143; }
        fi
        while read -r path key; do
            cat "$q/$key.err" >> "$state/logs/order.err" 2>/dev/null || :
            if [ ! -f "$q/$key.deps" ]; then
                echo "$path $key" >> "$q/failed"
                echo "$path order" >> "$state/failed"
                continue
            fi
            skip=$(sed -n 1p "$q/$key.deps")
            if [ -n "$skip" ]; then
                echo "$path $skip" >> "$state/skipped.new"
                continue
            fi
            cat "$q/$key.package" >> "$state/packages.new" || return 1
            echo "$path $path" >> "$state/edges.new"
            sed 1d "$q/$key.deps" | while read -r dep soft; do
                [ -n "$dep" ] || continue
                echo "$dep $path" >> "$state/edges.new"
                [ -z "$soft" ] || echo "$dep $path" >> "$state/soft.new"
                echo "$dep" >> "$state/next"
            done
        done < "$q/frontier-map"
        if [ -s "$q/failed" ]; then
            echo 'Refused: the dependencies of these packages could not be read:' >&2
            while read -r path key; do
                echo "$path order" >&2
                [ ! -s "$q/$key.err" ] || tail -n 5 "$q/$key.err" | sed 's/^/    /' >&2
            done < "$q/failed"
            return 1
        fi
        sort -u "$state/next" > "$state/queue"
    done
    [ "$order_stop" = 0 ] || { trap - HUP INT TERM; return 143; }
    refused=$(skipped_deps "$state/skipped.new" "$state/edges.new")
    if [ -n "$refused" ]; then
        echo 'Refused: these packages depend on packages the profile skips:' >&2
        echo "$refused" >&2
        return 1
    fi
    sort_edges "$state/edges.new" "$state/order.new" "$state/soft.new" || return 1
    if [ -n "$cachearg" ]; then
        if options_snapshot > "$cache/options.identity.tmp" &&
            mv "$cache/options.identity.tmp" "$cache/options.identity"; then
            :
        else
            rm -rf "$cache"
            cachearg=
            vars_context=
            echo 'Dependency queries completed, but cache validation failed; results will not be reused' >&2
        fi
    fi
    rm -f "$state/soft.new"
    mv "$state/skipped.new" "$state/skipped"
    mv "$state/edges.new" "$state/edges"
    mv "$state/order.new" "$state/order"
    mv "$state/packages.new" "$state/packages"
    rm -f "$state/queue" "$state/next"
    rm -rf "$q"
    [ "$order_stop" = 0 ] || { trap - HUP INT TERM; return 143; }
    echo "Ordered $(wc -l < "$state/order") packages: $state/order; skipped $(wc -l < "$state/skipped")"
    trap - HUP INT TERM
}

# Emit a compact selected-target/closure summary while the state lock is held.
selected_summary()
{
    summary_cachearg=$cachearg
    cachearg=
    if mk -C "$PKGSRCDIR/pkgtools/pkg_install" show-vars \
        VARNAMES='OPSYS MACHINE_ARCH MACHINE_GNU_PLATFORM' \
        < /dev/null > "$state/selected-target.tmp"; then
        cachearg=$summary_cachearg
    else
        summary_status=$?
        cachearg=$summary_cachearg
        rm -f "$state/selected-target.tmp"
        return "$summary_status"
    fi
    identity=$(cat "$state/selected-target.tmp") || return 1
    rm -f "$state/selected-target.tmp"
    set -f
    set -- $identity
    set +f
    [ "$#" -eq 3 ] || { echo 'Unexpected target identity metadata' >&2; return 1; }
    target_opsys=$1 target_arch=$2 target_platform=$3
    roots_count=$(awk 'NF && $1 !~ /^#/ { n++ } END { print n + 0 }' "$list") || return 1
    counts=$(awk '$1 ~ /@native$/ { n++ } END { print NR + 0, n + 0 }' "$state/order") || return 1
    set -f
    set -- $counts
    set +f
    [ "$#" -eq 2 ] || return 1
    closure_count=$1 native_count=$2
    echo "Target: $target_opsys $target_arch ($target_platform)"
    echo "Selected roots: $roots_count"
    echo "Dependency closure: $closure_count packages ($((closure_count - native_count)) target, $native_count native)"
}

selected_order()
{
    mode=order
    order
    mode=summary
    selected_summary
}

# UI callers can order selected roots and build their graph under one lock.
# The outer dispatcher owns the state lock across both canonical operations.
selected_build()
{
    mode=order
    order
    mode=summary
    selected_summary
    mode=build
    # Order's PBULK options cache is for dependency planning only. Standalone
    # build queries installed dependencies and keys its answers separately.
    cachearg=
    vars_cache=
    vars_context=
    build_vars_context=
    env_identity=
    native_pkg_info=
    target_pkg_info=
    target_settings=
    build
}

# True if the distinfo of a package lists distfiles and DISTDIR has each
# with its recorded size. A package without distinfo asks bmake.
have_distfiles()
{
    [ -f "$PKGSRCDIR/$1/distinfo" ] || return 1
    awk '$1 == "Size" && $3 == "=" && NF == 5 { sub(/^\(/, "", $2); sub(/\)$/, "", $2); print $2, $4; n++ }
        END { exit !n }' "$PKGSRCDIR/$1/distinfo" > "$state/sizes" || return 1
    while read -r file size; do
        [ -f "$distdir/$file" ] && [ "$(wc -c < "$distdir/$file" | tr -d ' ')" = "$size" ] || return 1
    done < "$state/sizes"
}

fetch()
{
    reuse_cache
    distdir=$(var pkgtools/pkg_install DISTDIR)
    case "$distdir" in /*) ;; *) echo "DISTDIR is not absolute: $distdir" >&2; return 1 ;; esac
    # A native tool that a target package needs gets the target settings,
    # as in build (build_native): cross-libtool refuses to parse without them.
    target_settings=$(var pkgtools/pkg_install CROSSTARGETSETTINGS)
    crossfed=$(awk '$1 ~ /@native$/ && $2 !~ /@native$/ { print $1 }' "$state/edges" | sort -u)
    while read -r path; do
        dir=${path%@native}
        have_distfiles "$dir" && continue
        set -f
        if [ "$dir" = "$path" ]; then set --
        elif printf '%s\n' "$crossfed" | grep -qFx "$path"; then set -- USE_CROSS_COMPILE=no $target_settings
        else set -- USE_CROSS_COMPILE=no; fi
        set +f
        option_cache_role "$path"
        skip=$(mk -C "$PKGSRCDIR/$dir" "$@" show-var VARNAME=PKG_SKIP_REASON < /dev/null) || skip=
        if [ -n "$skip" ]; then
            echo "$path $skip" >> "$state/skipped"
            continue
        fi
        log=$state/logs/fetch-$(echo "$path" | tr / _).log
        mk -C "$PKGSRCDIR/$dir" "$@" fetch < /dev/null > "$log" 2>&1 ||
            echo "$path fetch" >> "$state/failed-fetch"
    done < "$state/order"
    rm -f "$state/sizes"
}

# Resolve the same build variables used by the actual worker, then lock every
# selected WRKDIR across the supervised invocation.  The metadata cache uses
# the identical role, arguments and variable list as build_one/build_native.
stage_supervise()
{
    stage_mode=$1
    stage_nodes=$2
    stage_rows=$state/.stage-locks.$$.tsv
    stage_json=$state/.stage-locks.$$.json
    stage_manifest=$state/.stage-locked.$$.txt
    : > "$stage_rows"
    while IFS= read -r stage_node; do
        [ -n "$stage_node" ] || continue
        case "$stage_node" in
            *@native)
                stage_path=${stage_node%@native}
                vars_context=${build_vars_context:+$build_vars_context|native}
                set -f
                set -- USE_CROSS_COMPILE=no PACKAGES="$native_packages"
                ! grep -qFx "$stage_node" "$run/crossfed" || set -- "$@" $target_settings
                set +f ;;
            *) stage_path=$stage_node; vars_context=${build_vars_context:+$build_vars_context|target}; set -- ;;
        esac
        stage_values=$(pkg_vars "$stage_path" 'PKGNAME WRKDIR PKG_SKIP_REASON PKGBASE PKGVERSION_NOREV PKGFILE WRKOBJDIR' "$@") || return 1
        stage_work=$(printf '%s\n' "$stage_values" | sed -n 2p)
        stage_root=$(printf '%s\n' "$stage_values" | sed -n 7p)
        [ -n "$stage_root" ] || stage_root=$(dirname "$stage_work")
        case "$stage_root:$stage_work" in /*:/*) ;; *) echo "Invalid stage metadata for $stage_node" >&2; return 1 ;; esac
        printf '%s\t%s\n' "$stage_root" "$stage_work" >> "$stage_rows"
    done < "$stage_nodes"
    "$host_python" -B -c 'import json,sys; rows=[x.rstrip("\n").split("\t") for x in open(sys.argv[1])]; json.dump([{"root":a,"work":b} for a,b in rows],open(sys.argv[2],"w"))' "$stage_rows" "$stage_json" || return 1
    rm -f "$stage_rows"
    stage_script=$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")
    stage_shell=$(command -v "$controller_shell") || return 1
    case "$stage_shell" in /*) ;; *) echo 'Cannot resolve the selected controller shell' >&2; return 1 ;; esac
    state_lock_release_safe=no
    if "$host_python" -B -E -S "$(dirname "$stage_script")/stage-context-supervisor.py" --sdk-source "$sdk_source" --locks "$stage_json" --manifest "$stage_manifest" --bulk -- "$stage_shell" "$stage_script" "$stage_mode" "$list" "$state"; then stage_status=0; else stage_status=$?; fi
    # A normal supervisor exit proves that the owned session was drained.
    if [ "$stage_status" -lt 128 ]; then state_lock_release_safe=yes; fi
    rm -f "$stage_json"
    if [ "${stop:-0}" = 1 ] && [ "$stage_status" = 0 ]; then return 143; fi
    return "$stage_status"
}

# Package-level parallelism. CROSS_BULK_JOBS packages build at once (default:
# half the selected SDK CPU capacity). Their MAKE_JOBS shares one total budget,
# with remainder CPUs assigned to earlier ready workers; the scheduler keeps
# each allocation until that worker reports its result. MAKE_JOBS_SAFE=no
# recipes stay serial. CROSS_BULK_MAKE_JOBS sets an explicit per-package value
# and is checked against the total budget.
# CROSS_BULK_CPUS overrides the SDK's detected CPU count.
host_cpus()
{
    set --
    [ -z "${CROSS_BULK_CPUS:-}" ] || set -- --jobs "$CROSS_BULK_CPUS"
    selected=$(mk -C "$PKGSRCDIR/pkgtools/pkg_install" \
        show-vars VARNAMES='EMBERBSD_HOST_PYTHON EMBERBSD_SDK_SOURCE' < /dev/null) || {
        echo 'Cannot read the selected SDK source and host Python from MAKECONF' >&2
        exit 2
    }
    [ "$(printf '%s\n' "$selected" | wc -l | tr -d ' ')" = 2 ] || {
        echo 'Unexpected selected SDK source and host Python metadata' >&2
        exit 2
    }
    host_python=$(printf '%s\n' "$selected" | sed -n 1p)
    sdk_source=$(printf '%s\n' "$selected" | sed -n 2p)
    case "$host_python" in /*) ;; *) echo 'EMBERBSD_HOST_PYTHON is not an absolute selected tool path' >&2; exit 2 ;; esac
    case "$sdk_source" in /*) ;; *) echo 'EMBERBSD_SDK_SOURCE is not an absolute SDK source path' >&2; exit 2 ;; esac
    n=$("$host_python" -B -E -S "$sdk_source/scripts/resolve-jobs.py" "$@") || {
        echo 'The SDK could not resolve the available CPU count' >&2
        exit 2
    }
    count SDK_CPU_COUNT "$n"
    echo "$n"
}

count()
{
    case "$2" in ''|*[!0-9]*|0) echo "$1 must be a positive number: $2" >&2; exit 2 ;; esac
}

# A node starts only when every dependency node in STATE/edges is done, so
# a package never builds a dependency itself. pkgsrc's depends step would
# still build one in place (DEPENDS_TARGET in the dependency's directory and
# WRKDIR) when the installed package does not match the required pattern,
# such as a buildlink pin older than the built PKGREVISION; two dependents
# would then build in one WRKDIR at once. So every build run gets a
# DEPENDS_TARGET that no makefile defines: such a dependent fails at once,
# and its log names the dependency ("Verifying
# emberbsd-refuse-unpreflighted-dependency for ...").
# Only the scheduler writes the
# state files; a node reports its result in STATE/run/KEY.result, written to
# a temporary name and renamed. Compiles never wait for a lock; only the
# installs into a shared prefix do. pkgsrc runs every pkg_add as
# NATIVE_PKG_ADD_CMD, which the build points at STATE/run/pkg_add. That
# wrapper holds a directory lock in STATE/locks per package database (its
# first -K argument: the target sysroot or the native host prefix), so
# installs into one prefix never overlap. mkdir is atomic on macOS, NetBSD
# and Linux alike.
#
# A native package built before the native profile kept rc.d in the host
# prefix (Ports 6869ff45) recorded "# DIR: /etc/rc.d m" in its +DIRS. When
# pkg_add -U replaces it, its +DIRS REMOVE runs "rmdir -p /etc/rc.d"; on
# macOS that path is protected, and the attempt waits on a privacy prompt
# (TCC SystemPolicySysAdminFiles) with the database lock held. The native
# prefix never owns a path outside it, so before each install into the native
# database the wrapper marks every refcounted directory or file outside the
# recording package's prefix (its +CONTENTS @cwd) as preexisting, the state
# that pkgsrc's +DIRS and +FILES record for a path that existed before the
# package: REMOVE then leaves it alone. Target databases are not changed.
pkg_add_wrapper()
{
    native_info
    native_db=$(set -f; set -- $native_pkg_info
        while [ "$#" -gt 1 ]; do [ "$1" != -K ] || { echo "$2"; break; }; shift; done)
    cat <<WRAPPER
#!/bin/sh
# Written by scripts/cross-bulk.sh build: one pkg_add per package database.
locks=$state/locks
native_db='$native_db'
WRAPPER
    cat <<'WRAPPER'
db=default
prev=
for arg do
    if [ "$prev" = -K ]; then db=$arg; break; fi
    prev=$arg
done
lock=$locks/$(printf '%s' "$db" | tr -c 'A-Za-z0-9._-' '_')
until mkdir "$lock" 2> /dev/null; do sleep 1; done
trap 'rmdir "$lock"' EXIT
trap 'exit 143' TERM INT HUP
if [ -n "$native_db" ] && [ "$db" = "$native_db" ]; then
    grep -lE '^# (DIR|FILE): /' "$db"/*/+DIRS "$db"/*/+FILES 2> /dev/null |
    while read -r meta; do
        pkgdir=${meta%/*}
        prefix=$(sed -n '/^@cwd /{s/^@cwd //;p;q;}' "$pkgdir/+CONTENTS")
        case "$prefix" in /?*) ;; *) continue ;; esac
        case "$meta" in */+DIRS) kind=dirs ;; *) kind=files ;; esac
        sed -En 's/^# (DIR|FILE): (\/[^ ]*).*/\2/p' "$meta" |
        while read -r path; do
            case "$path" in "$prefix"|"$prefix"/*) continue ;; esac
            shadow=$db.refcount/$kind$path
            [ -d "$shadow" ] && [ ! -f "$shadow/+PREEXISTING" ] || continue
            echo "${pkgdir##*/}" > "$shadow/+PREEXISTING"
            echo "cross-bulk: $path is outside the native prefix of ${pkgdir##*/}; marked preexisting" >&2
        done
    done
fi
rc=0
WRAPPER
    printf '%s "$@" || rc=$?\nexit "$rc"\n' "$1"
}

dev_of()
{
    stat -c %d "$1" 2> /dev/null || stat -f %d "$1" 2> /dev/null
}

# The trash directory: WRKOBJDIR/.cross-bulk-trash, or none.
trash_init()
{
    trash=
    [ "${CROSS_BULK_TRASH:-yes}" != no ] || return 0
    wrkobj=$(var pkgtools/pkg_install WRKOBJDIR)
    case "$wrkobj" in /?*) ;; *) return 0 ;; esac
    mkdir -p "$wrkobj/.cross-bulk-trash" 2> /dev/null || return 0
    trash=$(CDPATH= cd -- "$wrkobj/.cross-bulk-trash" && pwd -P) || trash=
    case "$trash" in */.cross-bulk-trash) ;; *) trash= ; return 0 ;; esac
    trash_dev=$(dev_of "$trash") || trash=
}

# Rename an old WRKDIR ($1) of job $2 into the trash: one rename on the
# same file system. Fails (and the clean step removes it) when there is
# no trash or the WRKDIR is on another file system.
retire()
{
    [ -n "$trash" ] || return 1
    case "$1" in /?*) ;; *) return 1 ;; esac
    [ -d "$1" ] && [ ! -L "$1" ] || return 0
    [ "$(dev_of "$(dirname "$1")")" = "$trash_dev" ] || return 1
    mv "$1" "$trash/$stamp.$2" 2> /dev/null && [ ! -e "$1" ]
}

# Set clean to the clean target, or to nothing when the WRKDIR ($1) went to
# the trash: each target-mode bmake run parses the whole package again
# (seconds each), and the clean step would have nothing to remove.
# $2 names the job, $3 is the PKGPATH and the rest are its bmake arguments.
prepare_wrkdir()
{
    clean=clean
    prepare_wrkdir_error=
    if [ -e "$1" ] || [ -L "$1" ]; then
        work_identity=$("$host_python" -B -E -S "$(dirname "$0")/cross-bulk-workdir.py" "$1" "$base" "$version" "$name") || return 1
        case "$work_identity" in
        same)
            # 0 unchanged (keep), 1 changed (rebuild), other resolver error (keep, stop).
            work_dependencies_current "$@"
            case $? in
            0) clean=; return 0 ;;
            1) echo "$3: kept WRKDIR resolved other dependencies; building it again" >&2 ;;
            *) prepare_wrkdir_error=workdir-dependency-check; return 1 ;;
            esac
            ;;
        incompatible) ;;
        *) return 1 ;;
        esac
    fi
    if retire "$1" "$2"; then
        [ -e "$1" ] || [ -L "$1" ] || clean=
    else
        # Make it removable for the clean step: Go module caches are read-only.
        case "$1" in /*) [ ! -d "$1" ] || chmod -R u+w "$1" ;; esac
    fi
}


# pkgsrc lists the dependencies of a WRKDIR (.depends) and resolves them to
# installed packages (.rdepends) once, and a kept WRKDIR keeps both. When a
# dependency was replaced since then (expat-2.10.0 by expat-2.10.0nb1), the
# checks after the install compare the objects with the old names and refuse
# the newer package as no runtime dependency. List and resolve them again
# with the same bmake arguments ($1 WRKDIR, $2 job, $3 PKGPATH, then the
# arguments). Return 0 when both lists are unchanged, 1 when they changed
# (caller rebuilds), and 2 when the resolver fails (caller keeps WRKDIR,
# reports the cause, and stops the job).
work_dependencies_current()
{
    wdc_work=$1 wdc_check=$run/$2.dependencies wdc_path=$3
    shift 3
    if [ -f "$wdc_work/.rdepends" ]; then
        wdc_files='depends rdepends'
    elif [ -f "$wdc_work/.depends" ]; then
        wdc_files=depends
    else
        return 0
    fi
    rm -f "$wdc_check.depends" "$wdc_check.rdepends" "$wdc_check.log"
    if ! bmk -C "$PKGSRCDIR/$wdc_path" "$@" "_DEPENDS_FILE=$wdc_check.depends" \
        "_RDEPENDS_FILE=$wdc_check.rdepends" "$wdc_check.${wdc_files##* }" \
        < /dev/null > "$wdc_check.log" 2>&1; then
        echo "$wdc_path: kept WRKDIR dependency check failed:" >&2
        cat "$wdc_check.log" >&2
        rm -f "$wdc_check.depends" "$wdc_check.rdepends" "$wdc_check.log"
        return 2
    fi
    wdc_status=0
    for wdc_file in $wdc_files; do
        cmp -s "$wdc_check.$wdc_file" "$wdc_work/.$wdc_file"
        wdc_cmp=$?
        case $wdc_cmp in
        0) ;;
        1) wdc_status=1 ;;
        *)
            echo "$wdc_path: kept WRKDIR dependency check failed: cmp status $wdc_cmp" >&2
            rm -f "$wdc_check.depends" "$wdc_check.rdepends" "$wdc_check.log"
            return 2
            ;;
        esac
    done
    rm -f "$wdc_check.depends" "$wdc_check.rdepends" "$wdc_check.log"
    return "$wdc_status"
}
# Retire successful workdirs only when the caller opts in. If a rename to the
# same-filesystem trash is unavailable, use pkgsrc's scoped clean target.
# Cleanup trouble is diagnostic; it must not undo a completed installation.
clean_success()
{
    [ "${CROSS_BULK_CLEAN_SUCCESS:-no}" = yes ] || return 0
    if retire "$wrkdir" "$job.$key.complete" &&
        [ ! -e "$wrkdir" ] && [ ! -L "$wrkdir" ]; then
        return 0
    fi
    if ! bmk -C "$PKGSRCDIR/$path" "$@" clean < /dev/null >> "$log" 2>&1; then
        echo "WARNING: $node built successfully but WRKDIR cleanup failed: $wrkdir" >&2
        echo "WARNING: $node built successfully but WRKDIR cleanup failed: $wrkdir" >> "$log"
    fi
    return 0
}

# Remove the entries of the trash one at a time at the lowest priority,
# without following symbolic links, until the build ($1) is gone. Only
# this directory's own entries are removed.
deleter()
{
    set +e
    lowprio='nice -n 19'
    if [ -x /usr/sbin/taskpolicy ]; then
        lowprio="/usr/sbin/taskpolicy -b $lowprio"
    elif command -v ionice > /dev/null 2>&1; then
        lowprio="ionice -c 3 $lowprio"
    fi
    while kill -0 "$1" 2> /dev/null; do
        busy=
        for entry in "$trash"/* "$trash"/.[!.]*; do
            [ -e "$entry" ] || [ -L "$entry" ] || continue
            case "${entry#"$trash"/}" in ''|*/*) continue ;; esac
            if [ -d "$entry" ] && [ ! -L "$entry" ]; then
                $lowprio chmod -R u+w "$entry" 2> /dev/null || :
                $lowprio rm -rf "$entry" 2> /dev/null || :
            else
                rm -f "$entry" 2> /dev/null || :
            fi
            busy=1
            kill -0 "$1" 2> /dev/null || return 0
        done
        [ -n "$busy" ] || sleep 2
    done
}

# Heavy links. CROSS_BULK_LINK_LAUNCHER is the SDK heavy-link launcher
# prefix, as in the bootstrap mk.conf: "PYTHON SDK/scripts/link-launcher.py
# --lock-root LOCKS --context CONTEXT --", absolute words without spaces.
# Every build make gets it as EMBERBSD_DARWIN_LINKER_LAUNCHER and
# EMBERBSD_GCC16_LINKER_LAUNCHER, and as the CMake list
# EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER, so the recipes that honour them
# link one heavy group at a time across all CROSS_BULK_JOBS packages and
# every other build that shares CONTEXT. Unset: no launcher, as before.
link_args= link_list=
link_launcher_args()
{
    link_args=
    [ -n "${CROSS_BULK_LINK_LAUNCHER:-}" ] || return 0
    set -f
    set -- $CROSS_BULK_LINK_LAUNCHER
    set +f
    case "$1" in /*) ;; *) echo "CROSS_BULK_LINK_LAUNCHER must start with an absolute path" >&2; exit 2 ;; esac
    eval "last=\${$#}"
    [ "$last" = -- ] || { echo "CROSS_BULK_LINK_LAUNCHER must end with --" >&2; exit 2; }
    link_list=$(printf '%s;' "$@"); link_list=${link_list%;}
    link_args=yes
}

# Build runs: see the DEPENDS_TARGET note above.
bmk()
{
    if [ -n "$link_args" ]; then
        set -- EMBERBSD_DARWIN_LINKER_LAUNCHER="$CROSS_BULK_LINK_LAUNCHER" \
            EMBERBSD_GCC16_LINKER_LAUNCHER="$CROSS_BULK_LINK_LAUNCHER" \
            EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER="$link_list" "$@"
    fi
    mk MAKE_JOBS="$make_jobs" NATIVE_PKG_ADD_CMD="$run/pkg_add" \
        DEPENDS_TARGET=emberbsd-refuse-unpreflighted-dependency "$@"
}

# Configure under the existing stage lock before package compilation, so
# byte-identical Autoconf outputs retain their original input timestamps.
stage_configure_preserve()
{
    scp_path=$1 scp_work=$2
    shift 2
    if [ -n "$link_args" ]; then
        set -- --sdk-source "$sdk_source" --bmake "$BMAKE" --recipe "$PKGSRCDIR/$scp_path" \
            --expected-work "$scp_work" --make-arg "EMBERBSD_NATIVE_TOOLSET_VERIFIED=$native" \
            --make-arg "MAKE_JOBS=$make_jobs" --make-arg "NATIVE_PKG_ADD_CMD=$run/pkg_add" \
            --make-arg DEPENDS_TARGET=emberbsd-refuse-unpreflighted-dependency \
            ${cachearg:+--make-arg "PBULK_CACHE_DIRECTORY=$cachearg"} \
            --make-arg "EMBERBSD_DARWIN_LINKER_LAUNCHER=$CROSS_BULK_LINK_LAUNCHER" \
            --make-arg "EMBERBSD_GCC16_LINKER_LAUNCHER=$CROSS_BULK_LINK_LAUNCHER" \
            --make-arg "EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER=$link_list" -- "$@"
    else
        set -- --sdk-source "$sdk_source" --bmake "$BMAKE" --recipe "$PKGSRCDIR/$scp_path" \
            --expected-work "$scp_work" --make-arg "EMBERBSD_NATIVE_TOOLSET_VERIFIED=$native" \
            --make-arg "MAKE_JOBS=$make_jobs" --make-arg "NATIVE_PKG_ADD_CMD=$run/pkg_add" \
            --make-arg DEPENDS_TARGET=emberbsd-refuse-unpreflighted-dependency \
            ${cachearg:+--make-arg "PBULK_CACHE_DIRECTORY=$cachearg"} -- "$@"
    fi
    "$host_python" -B -E -S "$(dirname "$0")/stage-local-configure-preserve.py" "$@"
}

# Shared-pair target_install adds a stage-specific argument. Until that
# complete invocation can be replayed here, keep its ordinary configure path.
stage_configure_preserve_target()
{
    shared_pair_for_path "$1" || :
    if [ -n "$shared_pair" ] && [ "$(cat "$run/shared-$shared_pair/enabled" 2>/dev/null)" = yes ]; then
        if [ -e "$2/.emberbsd-configure-preserve.json" ] || [ -L "$2/.emberbsd-configure-preserve.json" ]; then
            echo 'Cannot change to a shared stage while configure recovery is pending' >&2
            return 1
        fi
        return 0
    fi
    if [ "$install" = package-install ]; then
        set -- "$@" PKGSRC_KEEP_BIN_PKGS=yes
    fi
    stage_configure_preserve "$@"
}

# Print device, inode, size, modification time and name of each path.
sigs()
{
    if [ "$gnu_stat" = yes ]; then
        stat -c '%d %i %s %.9Y %n' "$@" 2> /dev/null || :
    else
        stat -f '%d %i %z %Fm %N' "$@" 2> /dev/null || :
    fi
}

# The signatures of the makefiles $2 that a parse in package directory $1
# read, and of their directories.
makefile_sigs()
{
    mf_dir=$1 mf_files=$2
    (
        cd "$PKGSRCDIR/$mf_dir" || exit 1
        set -f
        set -- $(printf '%s\n' $mf_files | awk '{ print; d = $0
            if (!sub("/[^/]*$", "", d)) d = "."
            if (d == "") d = "/"
            print d }' | sort -u)
        sigs "$@"
    )
}

# The values of the variables $2 of package directory $1, one per line, as
# show-vars prints them; further arguments go to bmake. See STATE/vars above.
pkg_vars()
{
    vdir=$1 vnames=$2
    shift 2
    ventry=
    if [ -n "$vars_cache" ]; then
        vkey="$vdir $vnames $*"
        [ -z "$vars_context" ] || vkey="$vars_context|$vkey"
        ventry=$vars_cache/$(printf '%s\n' "$vkey" | cksum | tr ' ' _)
        if [ -f "$ventry" ] && [ -f "$ventry.sig" ] &&
            makefile_sigs "$vdir" "$(sed -n 1p "$ventry")" | cmp -s - "$ventry.sig"; then
            sed 1d "$ventry"
            return 0
        fi
        : > "$ventry.start"
    fi
    if [ "$mode" = deps ]; then
        vout=$(mk -C "$PKGSRCDIR/$vdir" "$@" _MAKEVARS_MK=defined show-vars \
            VARNAMES="$vnames .MAKE.MAKEFILES .CURDIR" < /dev/null) || return 1
    else
        vout=$(bmk -C "$PKGSRCDIR/$vdir" "$@" show-vars \
            VARNAMES="$vnames .MAKE.MAKEFILES .CURDIR" < /dev/null) || return 1
    fi
    vn=$(set -f; set -- $vnames; echo $#)
    if [ "$(printf '%s\n' "$vout" | wc -l | tr -d ' ')" != $((vn + 2)) ]; then
        # Not one line per variable: use it as it is, keep nothing.
        printf '%s\n' "$vout"
        return 0
    fi
    printf '%s\n' "$vout" | sed -n "1,${vn}p"
    vfiles=$(printf '%s\n' "$vout" | sed -n "$((vn + 1))p")
    [ -n "$ventry" ] && [ -n "$vfiles" ] || return 0
    vfiles_sig=$vfiles
    if [ "$mode" = deps ]; then
        # PBULK option files are validated as one cache context. The first
        # query can create them while bmake is parsing, so their directory
        # mtimes must not make otherwise valid package signatures miss.
        vfiles_sig=$(printf '%s\n' $vfiles | awk -v cache="$cache" '
            $0 == cache || index($0, cache "/") == 1 { next }
            !seen++ { printf "%s", $0; next }
            { printf " %s", $0 }
            END { if (seen) print "" }')
    fi
    # A makefile changed during the parse may not be what it read.
    (
        cd "$PKGSRCDIR/$vdir" || exit 1
        set -f
        [ -z "$(find $vfiles_sig -prune -newer "$ventry.start" 2> /dev/null)" ]
    ) || return 0
    makefile_sigs "$vdir" "$vfiles_sig" > "$ventry.sig.tmp" &&
        { printf '%s\n' "$vfiles_sig"; printf '%s\n' "$vout" | sed -n "1,${vn}p"; } > "$ventry.tmp" &&
        mv "$ventry.sig.tmp" "$ventry.sig" && mv "$ventry.tmp" "$ventry" || :
}

result()
{
    printf '%s\n' "$2" > "$run/$1.tmp"
    mv "$run/$1.tmp" "$run/$1.result"
    reported=1
}

# A native node is built and installed into the native host prefix as
# pkgsrc's depends step would do it. Its binary package goes to
# PACKAGES-native/All, not to PACKAGES/All, where the target package of
# the same PKGNAME is and where STATE/done has its record. One that a
# target package needs gets the target settings (CROSSTARGETSETTINGS)
# that pkgsrc passes to such a tool. It is done when the native prefix has
# a package of its PKGBASE at its version or newer. An older one is
# replaced (pkgsrc replace, pkg_add -U), which keeps the packages that
# depend on it registered. The replace runs alone (see exclusive).
stage_verify_locked()
{
    [ "$stage_supervised" = yes ] && [ -n "${CROSS_BULK_STAGE_MANIFEST:-}" ] &&
        [ -f "$CROSS_BULK_STAGE_MANIFEST" ] && grep -qFx "$1" "$CROSS_BULK_STAGE_MANIFEST"
}

build_native()
{
    vars_context=${build_vars_context:+$build_vars_context|native}
    nnode=$1 key=$2 path=${1%@native}
    set -f
    set -- USE_CROSS_COMPILE=no PACKAGES="$native_packages"
    ! grep -qFx "$nnode" "$run/crossfed" || set -- "$@" $target_settings
    set +f
    varnames='PKGNAME WRKDIR PKG_SKIP_REASON PKGBASE PKGVERSION_NOREV PKGFILE WRKOBJDIR'
    vars=$(pkg_vars "$path" "$varnames" "$@") ||
        { result "$key" "failed $nnode vars"; return 0; }
    name=$(printf '%s\n' "$vars" | sed -n 1p)
    wrkdir=$(printf '%s\n' "$vars" | sed -n 2p)
    stage_verify_locked "$wrkdir" || { result "$key" "failed $nnode stage-lock-mismatch"; return 0; }
    skip=$(printf '%s\n' "$vars" | sed -n 3p)
    base=$(printf '%s\n' "$vars" | sed -n 4p)
    version=$(printf '%s\n' "$vars" | sed -n 5p)
    log=$state/logs/$key.log
    if [ -n "$skip" ]; then
        result "$key" "skipped $nnode $skip"
        return 0
    fi
    stage_local_revision_changed=no
    if [ "${stage_local:-no}" = yes ]; then
        stage_local_identity=$(stage_local_preflight "$wrkdir" "$base" "$version" "$name" "$log") || {
            result "$key" "failed $nnode incompatible-stage"
            return 0
        }
        [ "$stage_local_identity" != revision ] || stage_local_revision_changed=yes
        set -- "$@" _EMBER_STAGE_LOCAL_PACKAGE_INSTALL=yes \
            "_EMBER_STAGE_LOCAL_PKGPATH=$path" \
            "_EMBER_STAGE_LOCAL_WRKDIR=$wrkdir"
    fi
    if [ "${stage_local:-no}" != yes ] && native_current "$base" "$name"; then
        result "$key" "done $nnode"
        return 0
    fi
    install=install why=build
    set -f
    [ -z "$base" ] || ! $native_pkg_info -e "$base" > /dev/null 2>&1 || install=replace
    set +f
    echo "$(date '+%Y-%m-%d %H:%M:%S') start $nnode $name" >> "$state/progress"
    if [ "${stage_local:-no}" != yes ]; then
        prepare_wrkdir "$wrkdir" "$job.$key" "$path" "$@" || { result "$key" "failed $nnode ${prepare_wrkdir_error:-incompatible-workdir}"; return 0; }
    fi
    : > "$log"
    if { [ "${stage_local:-no}" = yes ] || [ -z "$clean" ] || bmk -C "$PKGSRCDIR/$path" "$@" clean < /dev/null >> "$log" 2>&1; } &&
        { [ "${stage_local_revision_changed:-no}" != yes ] || bmk -C "$PKGSRCDIR/$path" "$@" depends-clean _package-clean < /dev/null >> "$log" 2>&1; } &&
        { [ "${stage_local:-no}" != yes ] || stage_configure_preserve "$path" "$wrkdir" "$@" _AUTOMATIC=yes >> "$log" 2>&1; } &&
        if [ "${package_only:-no}" = yes ]; then
            bmk -C "$PKGSRCDIR/$path" "$@" _AUTOMATIC=yes package < /dev/null >> "$log" 2>&1
        elif [ "$install" = replace ]; then
            bmk -C "$PKGSRCDIR/$path" "$@" _AUTOMATIC=yes package < /dev/null >> "$log" 2>&1 &&
                { native_replace_keeps_libraries "$base" "$native_packages/All/$name" >> "$log" 2>&1 ||
                    { why=replace; false; }; } &&
                if [ "${stage_local:-no}" = yes ]; then
                    bmk -C "$PKGSRCDIR/$path" "$@" _AUTOMATIC=yes replace < /dev/null >> "$log" 2>&1
                else
                    exclusive "$key" bmk -C "$PKGSRCDIR/$path" "$@" _AUTOMATIC=yes replace < /dev/null >> "$log" 2>&1
                fi
        else
            bmk -C "$PKGSRCDIR/$path" "$@" _AUTOMATIC=yes install < /dev/null >> "$log" 2>&1
        fi; then
        [ "${stage_local:-no}" = yes ] || clean_success "$@"
        echo "$(date '+%Y-%m-%d %H:%M:%S') done $nnode" >> "$state/progress"
        result "$key" "built $nnode $name native"
        return 0
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAILED $nnode" >> "$state/progress"
    result "$key" "failed $nnode $why"
}

# Whether replacing the installed native package $1 with the binary package
# $2 (a PKGNAME under PACKAGES-native/All, without its suffix) keeps every
# shared library that it provides now. pkgsrc replace (pkg_add -U) removes
# the files of the installed package mid-pass. A new major version that
# no longer has a library (OpenSSL 4 has libssl.4, not libssl.3) leaves the
# packages that depend on it loading a missing library: the native
# pkg_install among them, and with it every later build in the native
# prefix. Such an update needs its dependents rebuilt with it, between
# passes, so the build refuses the replace, names what it would break and
# keeps the package in PACKAGES-native/All. The PROVIDES and REQUIRES
# lines of the build information list the shared libraries.
native_replace_keeps_libraries()
{
    nr_file=
    for nr_f in "$2".t*; do [ -f "$nr_f" ] && { nr_file=$nr_f; break; }; done
    nr_old=$($native_pkg_info -q -Q PROVIDES "$1" 2> /dev/null) || nr_old=
    [ -n "$nr_old" ] || return 0
    nr_new=
    [ -z "$nr_file" ] || nr_new=$($native_pkg_info -q -Q PROVIDES "$nr_file" 2> /dev/null) || nr_new=
    nr_gone=$(printf '%s\n' "$nr_old" | NR_NEW="$nr_new" awk '
        BEGIN { n = split(ENVIRON["NR_NEW"], w, "\n"); for (i = 1; i <= n; i++) have[w[i]] = 1 }
        $0 != "" && !($0 in have)')
    [ -n "$nr_gone" ] || return 0
    nr_deps=$($native_pkg_info -q -R "$1" 2> /dev/null | awk 'NF') || nr_deps=
    [ -n "$nr_deps" ] || return 0
    echo "Refusing to replace $1 in the native prefix during a pass: ${nr_file:-$2} no longer provides"
    printf '    %s\n' $nr_gone
    echo "and these installed packages depend on $1:"
    for nr_d in $nr_deps; do
        nr_req=$($native_pkg_info -q -Q REQUIRES "$nr_d" 2> /dev/null | NR_GONE="$nr_gone" awk '
            BEGIN { n = split(ENVIRON["NR_GONE"], w, "\n"); for (i = 1; i <= n; i++) gone[w[i]] = 1 }
            $0 in gone' | tr '\n' ' ')
        echo "    $nr_d${nr_req:+ (loads $nr_req)}"
    done
    echo "Install the package and rebuild these between passes."
    return 1
}

# pkgsrc replace (pkg_add -U) deletes the files of the installed package
# before it adds those of the new one. A build that runs at that time in
# the same prefix or sysroot misses them: a tool linked with libffi from
# the native prefix fails with "Library not loaded" while libffi@native is
# replaced. A job therefore builds its package first and runs the replace,
# "$2"..., only when no other job runs: it asks with RUN/$1.replace and
# waits for RUN/$1.replacing, which build gives it when every running job
# waits so; build starts no job while one waits or replaces.
exclusive()
{
    xkey=$1
    shift
    : > "$run/$xkey.replace"
    until [ -f "$run/$xkey.replacing" ]; do sleep 1; done
    xrc=0
    "$@" || xrc=$?
    rm -f "$run/$xkey.replacing"
    return "$xrc"
}

# Run one target install through bmake. Preserve the binary package in the
# shared repository while cross-bulk installs it into the target sysroot.
target_install_bmake()
{
    tib_target=$1
    shift
    if [ "$tib_target" = package-install ]; then
        bmk "$@" PKGSRC_KEEP_BIN_PKGS=yes "$tib_target" < /dev/null
    else
        bmk "$@" "$tib_target" < /dev/null
    fi
}

# Run the install targets $2... of target package $1, one bmake run each.
target_install()
{
    tpath=$1
    shift
    shared_arg=
    shared_pair_for_path "$tpath" || :
    if [ -n "$shared_pair" ] && [ "$(cat "$run/shared-$shared_pair/enabled" 2>/dev/null)" = yes ]; then
        shared_stage=$(cat "$run/shared-$shared_pair/stage")
        shared_var=$(cat "$run/shared-$shared_pair/variable")
        if [ "$tpath" = "$shared_producer" ] ||
            { [ -f "$run/shared-$shared_pair/producer-ready" ] && [ -d "$shared_stage" ]; }; then
            shared_arg=$shared_var=$shared_stage
        fi
    fi
    for target do
        if [ -n "$shared_arg" ]; then
            if [ "${stage_local:-no}" = yes ]; then
                target_install_bmake "$target" -C "$PKGSRCDIR/$tpath" "$shared_arg" \
                    _EMBER_STAGE_LOCAL_PACKAGE_INSTALL=yes \
                    "_EMBER_STAGE_LOCAL_PKGPATH=$tpath" \
                    "_EMBER_STAGE_LOCAL_WRKDIR=$wrkdir" || return 1
            else
                target_install_bmake "$target" -C "$PKGSRCDIR/$tpath" "$shared_arg" || return 1
            fi
        else
            if [ "${stage_local:-no}" = yes ]; then
                target_install_bmake "$target" -C "$PKGSRCDIR/$tpath" \
                    _EMBER_STAGE_LOCAL_PACKAGE_INSTALL=yes \
                    "_EMBER_STAGE_LOCAL_PKGPATH=$tpath" \
                    "_EMBER_STAGE_LOCAL_WRKDIR=$wrkdir" || return 1
            else
                target_install_bmake "$target" -C "$PKGSRCDIR/$tpath" || return 1
            fi
        fi
    done
}

stage_local_preflight()
{
    sl_work=$1 sl_base=$2 sl_version=$3 sl_name=$4 sl_log=$5
    [ -n "$host_python" ] && [ -x "$host_python" ] || {
        echo 'Stage-local recovery requires the selected SDK Python' >> "$sl_log"
        return 1
    }
    "$host_python" -B -E -S "$PKGSRCDIR/mk/package/repackage-metadata-check.py" \
        --stage-local "$sl_work" "$sl_base" "$sl_version" "$sl_name" 2>> "$sl_log"
}

# A split package pair can share one compiled payload when the run contains
# both roles and their expanded source, toolchain and build inputs agree.
# Pair-specific data is kept here; staging and lifecycle are shared below.
shared_pair_for_path()
{
    case "$1" in
        lang/llvm|lang/llvm-devel) shared_pair=llvm ;;
        x11/qt6-qtbase|x11/qt6-qtbase-devel) shared_pair=qtbase ;;
        textproc/icu|textproc/icu-devel) shared_pair=icu ;;
        *) shared_pair= ;;
    esac
    [ -z "$shared_pair" ] || shared_pair_config "$shared_pair"
}

shared_pair_config()
{
    pair_key=$1
    shared_pair=$pair_key
    shared_producer=
    shared_consumer=
    shared_variable=
    shared_stage_name=
    shared_fields=
    case "$pair_key" in
        llvm)
            shared_producer=lang/llvm
            shared_consumer=lang/llvm-devel
            shared_variable=EMBERBSD_LLVM_SHARED_STAGE
            shared_stage_name=.llvm-shared-full-stage
            shared_fields='LLVM_VERSION WRKSRC DISTNAME PATCHDIR DISTINFO_FILE PKG_OPTIONS USE_CROSS_COMPILE OPSYS OS_VERSION MACHINE_ARCH MACHINE_GNU_PLATFORM EMBERBSD_CROSS_COMPILER_FAMILY PKGSRC_COMPILER CMAKE CFLAGS CXXFLAGS CPPFLAGS LDFLAGS CMAKE_CONFIGURE_ARGS CONFIGURE_ENV CC CXX CPP AR AS LD RANLIB STRIP CWRAPPERS_APPEND.cc CWRAPPERS_APPEND.cxx _CMAKE_BUILD_SYSTEM INSTALL_TARGET CMAKE_INSTALL_ARGS LLVM_TARGETS_TO_BUILD LLVM_EXPERIMENTAL_TARGETS_TO_BUILD CROSS_DESTDIR TOOLDIR TOOL_PYTHONBIN EMBERBSD_LLVM_NATIVE_TOOLS MAKE_ENV CMAKE_BUILD_ARGS _CMAKE_BUILD_TOOL CMAKE_GENERATOR CMAKE_BUILD_DIR BUILD_TARGET BUILD_DIRS CONFIGURE_DIR SETENV RUN INSTALL_ENV INSTALL_DIRS PREFIX' ;;
        icu)
            shared_producer=textproc/icu
            shared_consumer=textproc/icu-devel
            shared_variable=EMBERBSD_ICU_SHARED_STAGE
            shared_stage_name=.icu-shared-full-stage
            shared_fields='DISTNAME PKGVERSION_NOREV WRKSRC PATCHDIR DISTINFO_FILE FILESDIR PKG_OPTIONS USE_CROSS_COMPILE OPSYS OS_VERSION MACHINE_ARCH MACHINE_GNU_PLATFORM ABI EMBERBSD_HOST_PREFIX EMBERBSD_NATIVE_TOOLSET_MAKECONF EMBERBSD_CROSS_COMPILER_FAMILY PKGSRC_COMPILER CFLAGS CXXFLAGS CPPFLAGS LDFLAGS CONFIGURE_ARGS CONFIGURE_ENV CC CXX CPP AR AS LD RANLIB STRIP CWRAPPERS_APPEND.cc CWRAPPERS_APPEND.cxx ICU_CROSS_DIR ICU_STAGED_HOST_PREFIX CROSS_DESTDIR TOOLDIR TOOL_PYTHONBIN EMBERBSD_CROSS_BUILD_PYTHON MAKE_ENV MAKE_FLAGS BUILD_TARGET BUILD_DIRS CONFIGURE_DIR SETENV RUN INSTALL_ENV INSTALL_DIRS PREFIX' ;;
        qtbase)
            shared_producer=x11/qt6-qtbase
            shared_consumer=x11/qt6-qtbase-devel
            shared_variable=EMBERBSD_QTBASE_SHARED_STAGE
            shared_stage_name=.qtbase-shared-full-stage
            shared_fields='QTVERSION WRKSRC DISTNAME PATCHDIR DISTINFO_FILE FILESDIR PKG_OPTIONS USE_CROSS_COMPILE OPSYS OS_VERSION MACHINE_ARCH MACHINE_GNU_PLATFORM EMBERBSD_CROSS_COMPILER_FAMILY PKGSRC_COMPILER CMAKE CFLAGS CXXFLAGS CPPFLAGS LDFLAGS CMAKE_CONFIGURE_ARGS CONFIGURE_ARGS CONFIGURE_ENV CC CXX CPP AR AS LD RANLIB STRIP CWRAPPERS_APPEND.cc CWRAPPERS_APPEND.cxx _CMAKE_BUILD_SYSTEM INSTALL_TARGET CMAKE_INSTALL_ARGS CROSS_DESTDIR TOOLDIR TOOL_PYTHONBIN EMBERBSD_NATIVE_TOOLSET_MAKECONF EMBERBSD_WAYLAND_SCANNER MAKE_ENV CMAKE_BUILD_ARGS _CMAKE_BUILD_TOOL CMAKE_GENERATOR CMAKE_BUILD_DIR BUILD_TARGET BUILD_DIRS CONFIGURE_DIR SETENV RUN INSTALL_ENV INSTALL_DIRS PREFIX QTPREFIX' ;;
    esac
}

shared_pair_configure_one()
{
    shared_pair_config "$1"
    shared_dir=$run/shared-$shared_pair
    mkdir -p "$shared_dir"
    printf '%s\n' no > "$shared_dir/enabled"
    printf '%s\n' "$shared_variable" > "$shared_dir/variable"
    grep -qFx "$shared_producer" "$state/order" && grep -qFx "$shared_consumer" "$state/order" || return 0
    shared_runtime_wrkdir=$(pkg_vars "$shared_producer" WRKDIR _MAKEVARS_MK=defined) || {
        echo "$shared_pair producer inputs could not be queried; using ordinary package builds" >&2
        return 0
    }
    case "$shared_runtime_wrkdir" in /*) ;; *)
        echo "$shared_pair producer WRKDIR is not absolute; using ordinary package builds" >&2
        return 0 ;;
    esac
    shared_runtime_values=$(pkg_vars "$shared_producer" "$shared_fields" "WRKDIR=$shared_runtime_wrkdir" _MAKEVARS_MK=defined) || {
        echo "$shared_pair producer compile inputs could not be queried; using ordinary package builds" >&2
        return 0
    }
    shared_devel_values=$(pkg_vars "$shared_consumer" "$shared_fields" "WRKDIR=$shared_runtime_wrkdir" _MAKEVARS_MK=defined) || {
        echo "$shared_pair consumer compile inputs could not be queried; using ordinary package builds" >&2
        return 0
    }
    set -f
    set -- $shared_fields
    shared_expected=$#
    set +f
    if [ "$(printf '%s\n' "$shared_runtime_values" | wc -l | tr -d ' ')" != "$shared_expected" ] ||
        [ "$(printf '%s\n' "$shared_devel_values" | wc -l | tr -d ' ')" != "$shared_expected" ]; then
        echo "$shared_pair compile inputs are incomplete; using ordinary package builds" >&2
        return 0
    fi
    [ "$shared_runtime_values" = "$shared_devel_values" ] || {
        echo "$shared_pair producer and consumer compile inputs differ; using ordinary package builds" >&2
        return 0
    }
    shared_stage=$shared_runtime_wrkdir/$shared_stage_name
    printf '%s\n' "$shared_stage" > "$shared_dir/stage"
    printf '%s\n' "$shared_runtime_wrkdir" > "$shared_dir/wrkdir"
    printf '%s\n' "$shared_producer" > "$shared_dir/producer"
    printf '%s\n' "$shared_consumer" > "$shared_dir/consumer"
    printf '%s\n' yes > "$shared_dir/enabled"
    echo "$shared_pair producer and consumer compile inputs match; enabling same-run stage handoff"
}

shared_pairs_configure()
{
    [ "${CROSS_BULK_SHARED_BUILD:-yes}" = yes ] || return 0
    shared_pair_configure_one llvm
    shared_pair_configure_one qtbase
    shared_pair_configure_one icu
}

# A single-package invocation can also restore the producer or package its
# consumer. Lock that possible pair before evaluating the shared-stage inputs;
# a disabled/incompatible pair may use only the requested stage afterwards.
package_stage_nodes()
(
    printf '%s\n' "$requested"
    case "$mode:$role:${CROSS_BULK_SHARED_BUILD:-yes}" in
        package:target:yes|package-install:target:yes) ;;
        *) return 0 ;;
    esac
    shared_pair_for_path "$path"
    [ -n "$shared_pair" ] || return 0
    grep -qFx "$shared_producer" "$state/order" &&
        grep -qFx "$shared_consumer" "$state/order" || return 0
    if [ "$path" = "$shared_producer" ]; then
        printf '%s\n' "$shared_consumer"
    else
        printf '%s\n' "$shared_producer"
    fi
)

shared_pair_ready()
{
    shared_pair_for_path "$1"
    [ -n "$shared_pair" ] && [ "$(cat "$run/shared-$shared_pair/enabled" 2>/dev/null)" = yes ] &&
        [ -f "$run/shared-$shared_pair/producer-ready" ] &&
        [ -d "$(cat "$run/shared-$shared_pair/stage")" ]
}

shared_pair_producer_succeeded()
{
    shared_pair_for_path "$1"
    [ -n "$shared_pair" ] && [ "$1" = "$shared_producer" ] || return 0
    shared_dir=$run/shared-$shared_pair
    [ "$(cat "$shared_dir/enabled" 2>/dev/null)" = yes ] || return 0
    shared_stage=$(cat "$shared_dir/stage")
    if [ -d "$shared_stage" ]; then
        : > "$shared_dir/producer-ready.tmp"
        mv "$shared_dir/producer-ready.tmp" "$shared_dir/producer-ready"
        echo "$shared_pair complete stage retained at $shared_stage"
    else
        echo "$shared_pair producer completed without a complete stage; consumer will build normally"
    fi
}

# Restore a completed producer's full stage without rebuilding or reinstalling
# its package. All temporary producer variables stay inside this subshell.
shared_pair_restore_producer()
(
    shared_pair_for_path "$1"
    [ -n "$shared_pair" ] && [ "$1" = "$shared_consumer" ] || return 0
    shared_dir=$run/shared-$shared_pair
    [ "$(cat "$shared_dir/enabled" 2>/dev/null)" = yes ] || return 0
    [ ! -f "$shared_dir/producer-ready" ] || return 0
    shared_stage=$(cat "$shared_dir/stage") || return 1
    producer_vars=$(pkg_vars "$shared_producer" 'PKGNAME WRKDIR PKGBASE PKGVERSION_NOREV') || return 0
    producer_name=$(printf '%s\n' "$producer_vars" | sed -n 1p)
    producer_work=$(printf '%s\n' "$producer_vars" | sed -n 2p)
    producer_base=$(printf '%s\n' "$producer_vars" | sed -n 3p)
    producer_version=$(printf '%s\n' "$producer_vars" | sed -n 4p)
    [ "$producer_work" = "$(cat "$shared_dir/wrkdir")" ] || return 0
    [ -f "$packages/$producer_name.tgz" ] || return 0
    awk -v p="$shared_producer" -v n="$producer_name" '$1 == p && $2 == n { ok=1; exit } END { exit !ok }' "$state/done" || return 0
    producer_build=$producer_work/.build_done
    [ ! -L "$producer_build" ] && [ -f "$producer_build" ] || return 0
    [ "$(cat "$producer_build")" = "$producer_name" ] || return 0
    stage_local_preflight "$producer_work" "$producer_base" "$producer_version" "$producer_name" "$log" >/dev/null || return 0
    stage_verify_locked "$producer_work" || {
        echo "Producer stage is not locked: $producer_work" >> "$log"
        return 1
    }
    stage_local=yes
    wrkdir=$producer_work
    shared_pair_reset_stage "$shared_producer" >> "$log" 2>&1 || return 1
    target_install "$shared_producer" stage-install >> "$log" 2>&1 || return 1
    shared_pair_producer_succeeded "$shared_producer" >> "$log" 2>&1
    shared_pair_ready "$shared_producer"
)

shared_pair_retire_producer()
{
    [ "${package_only:-no}" != yes ] || return 0
    shared_pair_for_path "$1"
    [ -n "$shared_pair" ] && [ "$1" = "$shared_consumer" ] || return 0
    shared_dir=$run/shared-$shared_pair
    [ "$(cat "$shared_dir/enabled" 2>/dev/null)" = yes ] && [ -f "$shared_dir/producer-ready" ] || return 0
    [ "${CROSS_BULK_CLEAN_SUCCESS:-no}" = yes ] || return 0
    shared_wrkdir=$(cat "$shared_dir/wrkdir")
    if retire "$shared_wrkdir" "$job.$shared_pair-after-consumer" &&
        [ ! -e "$shared_wrkdir" ] && [ ! -L "$shared_wrkdir" ]; then
        return 0
    fi
    shared_variable=$(cat "$shared_dir/variable")
    if ! bmk -C "$PKGSRCDIR/$shared_producer" "WRKDIR=$shared_wrkdir" \
        "$shared_variable=$(cat "$shared_dir/stage")" clean < /dev/null >> "$log" 2>&1; then
        echo "WARNING: $node built successfully but $shared_pair producer WRKDIR cleanup failed: $shared_wrkdir" >&2
        echo "WARNING: $node built successfully but $shared_pair producer WRKDIR cleanup failed: $shared_wrkdir" >> "$log"
    fi
    return 0
}

shared_pair_reset_stage()
{
    shared_pair_for_path "$1"
    [ "${stage_local:-no}" = yes ] && [ -n "$shared_pair" ] || return 0
    [ "$(cat "$run/shared-$shared_pair/enabled" 2>/dev/null)" = yes ] || return 0
    reset_work=$(pkg_vars "$1" WRKDIR) || return 1
    stage_verify_locked "$reset_work" || {
        echo "Shared package stage is not locked: $reset_work" >&2
        return 1
    }
    bmk -C "$PKGSRCDIR/$1" _package-clean < /dev/null || return 1
    if [ "$1" = "$shared_producer" ]; then
        shared_dir=$run/shared-$shared_pair
        shared_stage=$(cat "$shared_dir/stage")
        shared_wrkdir=$(cat "$shared_dir/wrkdir")
        [ "$shared_stage" = "$shared_wrkdir/$shared_stage_name" ] || return 1
        if [ -L "$shared_stage" ]; then
            echo "Refusing symlinked $shared_pair producer stage: $shared_stage" >&2
            return 1
        fi
        if [ -e "$shared_stage" ]; then
            [ -d "$shared_stage" ] || return 1
            rm -r "$shared_stage" || return 1
        fi
    fi
}

build_one()
{
    vars_context=${build_vars_context:+$build_vars_context|target}
    path=$1
    key=$(echo "$path" | tr / _)
    case "$path" in *@native) build_native "$path" "$key"; return 0 ;; esac
    log=$state/logs/$key.log
    varnames='PKGNAME WRKDIR PKG_SKIP_REASON PKGBASE PKGVERSION_NOREV PKGFILE WRKOBJDIR'
    vars=$(pkg_vars "$path" "$varnames") ||
        { result "$key" "failed $path vars"; return 0; }
    name=$(printf '%s\n' "$vars" | sed -n 1p)
    wrkdir=$(printf '%s\n' "$vars" | sed -n 2p)
    stage_verify_locked "$wrkdir" || { result "$key" "failed $path stage-lock-mismatch"; return 0; }
    skip=$(printf '%s\n' "$vars" | sed -n 3p)
    base=$(printf '%s\n' "$vars" | sed -n 4p)
    version=$(printf '%s\n' "$vars" | sed -n 5p)
    package_file=$(printf '%s\n' "$vars" | sed -n 6p)
    if [ -n "$skip" ]; then
        result "$key" "skipped $path $skip"
        return 0
    fi
    if [ "${stage_local:-no}" = yes ]; then
        stage_local_identity=$(stage_local_preflight "$wrkdir" "$base" "$version" "$name" "$log") || {
            result "$key" "failed $path incompatible-stage"
            return 0
        }
        stage_local_revision_changed=no
        [ "$stage_local_identity" != revision ] || stage_local_revision_changed=yes
        : > "$log"
        shared_pair_for_path "$path"
        if [ -n "$shared_pair" ] && [ "$(cat "$run/shared-$shared_pair/enabled" 2>/dev/null)" = yes ]; then
            if [ "$stage_local_revision_changed" = yes ] &&
                ! bmk -C "$PKGSRCDIR/$path" depends-clean < /dev/null >> "$log" 2>&1; then
                result "$key" "failed $path depends-clean"
                return 0
            fi
            if ! shared_pair_reset_stage "$path"; then
                result "$key" "failed $path shared-stage-cleanup"
                return 0
            fi
        elif [ "$stage_local_revision_changed" = yes ] &&
            ! bmk -C "$PKGSRCDIR/$path" depends-clean _package-clean < /dev/null >> "$log" 2>&1; then
            result "$key" "failed $path package-clean"
            return 0
        fi
    fi
    # The SDK supplies only authenticated catalog selections. Recheck the
    # exact name in pkgdb; ordinary source builds retain their existing policy.
    if [ "${stage_local:-no}" != yes ] && [ -n "${CROSS_BULK_BINARY_INSTALLED:-}" ] &&
        awk -v p="$path" -v n="$name" '$1 == p && $2 == n { f = 1 } END { exit !f }' "$CROSS_BULK_BINARY_INSTALLED"; then
        set -f
        if $target_pkg_info -e "$name" > /dev/null 2>&1; then
            set +f
            result "$key" "done $path"
            return 0
        fi
        set +f
    fi
    if [ "${stage_local:-no}" != yes ] && [ -f "$package_file" ] &&
        awk -v p="$path" -v n="$name" '$1 == p && $2 == n { f = 1; exit } END { exit !f }' "$state/done"; then
        shared_pair_retire_producer "$path"
        result "$key" "done $path"
        return 0
    fi
    [ "${stage_local:-no}" = yes ] || : > "$log"
    if [ "${stage_local:-no}" != yes ] && ! shared_pair_restore_producer "$path"; then
        result "$key" "failed $path shared-stage-restore"
        return 0
    fi
    # One short line per append: O_APPEND keeps concurrent lines whole.
    echo "$(date '+%Y-%m-%d %H:%M:%S') start $path $name MAKE_JOBS=$make_jobs" >> "$state/progress"
    if [ "${stage_local:-no}" != yes ]; then
        prepare_wrkdir "$wrkdir" "$job.$key" "$path" || { result "$key" "failed $path ${prepare_wrkdir_error:-incompatible-workdir}"; return 0; }
    fi
    # One package-install run does depends, package and the install: each
    # target-mode bmake run parses the package again, and the pkg_add wrapper
    # already keeps installs into one database apart. The sysroot may
    # already have another version of the package, such as one from an
    # earlier pass before a PKGREVISION bump; pkg_add refuses to install
    # over it, so the package is built and then replaced (pkgsrc replace,
    # pkg_add -U), which keeps the packages that depend on it registered.
    # The replace runs alone (see exclusive).
    install=package-install
    replace=
    set -f
    if [ "${package_only:-no}" = yes ]; then
        install=package
    else
        [ -z "$base" ] || ! $target_pkg_info -e "$base" > /dev/null 2>&1 || { install=package; replace=replace; }
    fi
    set +f
    { [ "${stage_local:-no}" = yes ] || [ -z "$clean" ] || bmk -C "$PKGSRCDIR/$path" clean < /dev/null >> "$log" 2>&1; } &&
        { [ "${stage_local:-no}" != yes ] || stage_configure_preserve_target "$path" "$wrkdir" \
            _EMBER_STAGE_LOCAL_PACKAGE_INSTALL=yes \
            "_EMBER_STAGE_LOCAL_PKGPATH=$path" "_EMBER_STAGE_LOCAL_WRKDIR=$wrkdir" >> "$log" 2>&1; } &&
        target_install "$path" $install >> "$log" 2>&1 &&
        { [ -z "$replace" ] || if [ "${stage_local:-no}" = yes ]; then
            target_install "$path" replace >> "$log" 2>&1
        else
            exclusive "$key" target_install "$path" replace >> "$log" 2>&1
        fi; } && {
        if [ ! -f "$package_file" ]; then
            echo "Package archive is missing: $package_file" >> "$log"
            result "$key" "failed $path package-archive"
            return 0
        fi
        if ! digest_output=$(shasum -a 256 "$package_file" 2>> "$log"); then
            echo "Could not compute SHA256 for package archive: $package_file" >> "$log"
            result "$key" "failed $path package-hash"
            return 0
        fi
        package_sha=${digest_output%% *}
        case "$package_sha" in
            ''|*[!0123456789abcdef]*)
                echo "Invalid SHA256 output for package archive: $package_file" >> "$log"
                result "$key" "failed $path package-hash"
                return 0 ;;
        esac
        if [ "${#package_sha}" -ne 64 ]; then
            echo "Invalid SHA256 output for package archive: $package_file" >> "$log"
            result "$key" "failed $path package-hash"
            return 0
        fi
        shared_pair_producer_succeeded "$path"
        if ! shared_pair_ready "$path"; then
            [ "${stage_local:-no}" = yes ] || clean_success
        fi
        shared_pair_retire_producer "$path"
        echo "$(date '+%Y-%m-%d %H:%M:%S') done $path" >> "$state/progress"
        result "$key" "built $path $name $package_sha"
        return 0
    }
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAILED $path" >> "$state/progress"
    result "$key" "failed $path build"
}

# Install one explicitly selected target or native package from its existing
# WRKDIR. This is an owner-directed recovery path, not archive reuse: it runs
# the same package-install/package+replace targets as build_one/build_native,
# while preserving the workdir and updating only this PKGPATH in state.
state_path_row()
{
    spr_file=$1 spr_path=$2 spr_row=${3:-}
    spr_tmp=$spr_file.tmp.$$
    [ -f "$spr_file" ] || : > "$spr_file"
    awk -v p="$spr_path" '$1 != p { print }' "$spr_file" > "$spr_tmp"
    [ -z "$spr_row" ] || printf '%s\n' "$spr_row" >> "$spr_tmp"
    mv "$spr_tmp" "$spr_file"
}

package_install_one()
{
    requested=$list
    package_only=no
    [ "$mode" != package ] || package_only=yes
    case "$requested" in
        *@native) role=native; path=${requested%@native} ;;
        *@native*) echo 'Expected one PKGPATH with an optional @native suffix' >&2; return 2 ;;
        *) role=target; path=$requested ;;
    esac
    case "$path" in
        ''|/*|*@native|*..*|*\ *) echo 'Expected one safe PKGPATH' >&2; return 2 ;;
    esac
    [ -f "$state/order" ] && grep -qFx "$requested" "$state/order" || {
        echo "PKGPATH is not in the saved order: $requested" >&2
        return 2
    }
    run=
    recovery_cleanup()
    {
        [ -z "$run" ] || rm -rf "$run"
        if [ "$state_lock_release_safe" = yes ]; then state_lock_release; else
            echo "Workers may not have drained; keeping lock $state/build.lock" >&2
        fi
    }
    trap recovery_cleanup EXIT
    stop=0
    trap 'stop=1' HUP INT TERM
    run=$(mktemp -d "$state/package-install.run.XXXXXX") || {
        echo 'Cannot create a private package-install run directory' >&2
        return 2
    }
    mkdir -p "$state/locks"
    vars_cache=
    gnu_stat=no
    clean=clean
    stage_local=yes
    job=1
    host_cpus > "$run/cpu-capacity"
    cpus=$(cat "$run/cpu-capacity")
    make_jobs=${CROSS_BULK_MAKE_JOBS:-$cpus}
    count CROSS_BULK_MAKE_JOBS "$make_jobs"
    if [ "$make_jobs" -gt "$cpus" ]; then
        echo "CROSS_BULK_MAKE_JOBS=$make_jobs exceeds resolved CPU capacity=$cpus" >&2
        return 2
    fi
    real_pkg_add=$(var pkgtools/pkg_install NATIVE_PKG_ADD_CMD)
    [ -n "$real_pkg_add" ] || { echo 'NATIVE_PKG_ADD_CMD is empty' >&2; return 2; }
    pkg_add_wrapper "$real_pkg_add" > "$run/pkg_add"
    chmod +x "$run/pkg_add"
    target_pkg_info=$(var pkgtools/pkg_install PKG_INFO)
    [ -n "$target_pkg_info" ] || { echo 'The target PKG_INFO is empty' >&2; return 2; }
    if [ "$role" = native ]; then native_info; fi
    packages=$(var pkgtools/pkg_install PACKAGES)/All
    native_packages=${packages%/All}-native
    target_settings=$(var pkgtools/pkg_install CROSSTARGETSETTINGS)
    if [ "$role" = native ]; then
        awk '$1 ~ /@native$/ && $2 !~ /@native$/ { print $1 }' "$state/edges" | sort -u > "$run/crossfed"
    else
        : > "$run/crossfed"
    fi
    stamp=$(date '+%Y%m%d%H%M%S')
    link_launcher_args
    if [ "$stage_supervised" = no ]; then
        package_stage_nodes > "$run/stage-node" || return 1
        stage_supervise "$mode" "$run/stage-node"
        return $?
    fi
    if [ "$mode" = buildlink-refresh ]; then
        : "${CROSS_BULK_LOCKED_WORK:?Missing supervised stage identity}"
        : "${CROSS_BULK_BUILDLINK_PROVIDER:?Select a buildlink provider}"
        set -- --bmake "$BMAKE" --recipe "$PKGSRCDIR/$path" \
            --expected-work "$CROSS_BULK_LOCKED_WORK" --provider "$CROSS_BULK_BUILDLINK_PROVIDER"
        if [ "$role" = native ]; then
            set -- "$@" --native --make-arg USE_CROSS_COMPILE=no --make-arg "PACKAGES=$native_packages"
            if grep -qFx "$requested" "$run/crossfed"; then
                set -f
                for stage_arg in $target_settings; do set -- "$@" --make-arg "$stage_arg"; done
                set +f
            fi
        fi
        "$host_python" -B -E -S "$(dirname "$0")/stage-local-buildlink-refresh.py" "$@"
        return $?
    fi
    if [ "$mode" = patch-refresh ]; then
        : "${CROSS_BULK_LOCKED_WORK:?Missing supervised stage identity}"
        set -- --sdk-source "$sdk_source" --bmake "$BMAKE" --recipe "$PKGSRCDIR/$path" \
            --expected-work "$CROSS_BULK_LOCKED_WORK"
        if [ "$role" = native ]; then
            set -- "$@" --native --make-arg "PACKAGES=$native_packages"
            if grep -qFx "$requested" "$run/crossfed"; then
                set -f
                for stage_arg in $target_settings; do set -- "$@" --make-arg "$stage_arg"; done
                set +f
            fi
        fi
        if [ -n "${CROSS_BULK_PATCH_REFRESH_PREVIOUS_RECIPE:-}" ]; then
            case "$CROSS_BULK_PATCH_REFRESH_PREVIOUS_RECIPE" in
                /*) ;;
                *) echo 'Previous recipe must be an absolute path' >&2; return 2 ;;
            esac
            set -- "$@" --previous-recipe "$CROSS_BULK_PATCH_REFRESH_PREVIOUS_RECIPE"
        fi
        if [ -n "${CROSS_BULK_PATCH_REFRESH_EXISTING_SUBST:-}" ]; then
            set -- "$@" --refresh-subst "$CROSS_BULK_PATCH_REFRESH_EXISTING_SUBST"
        fi
        if [ -n "${CROSS_BULK_PATCH_REFRESH_PREVIOUS_MAKEFILE:-}${CROSS_BULK_PATCH_REFRESH_SUBST_CLASS:-}" ]; then
            : "${CROSS_BULK_PATCH_REFRESH_PREVIOUS_MAKEFILE:?select the installed Makefile preimage}"
            : "${CROSS_BULK_PATCH_REFRESH_SUBST_CLASS:?select the added pre-configure SUBST class}"
            case "$CROSS_BULK_PATCH_REFRESH_PREVIOUS_MAKEFILE" in
                /*) ;;
                *) echo 'Previous Makefile must be an absolute path' >&2; return 2 ;;
            esac
            set -- "$@" --previous-makefile "$CROSS_BULK_PATCH_REFRESH_PREVIOUS_MAKEFILE" \
                --subst-class "$CROSS_BULK_PATCH_REFRESH_SUBST_CLASS"
        fi
        "$host_python" -B -E -S "$(dirname "$0")/stage-local-patch-refresh.py" "$@"
        return $?
    fi
    key=$(echo "$requested" | tr / _)
    node=$requested
    reported=
    if [ "$role" = target ]; then
        shared_pairs_configure
    fi
    [ "$stop" = 0 ] || return 143
    if [ "$role" = native ]; then build_native "$requested" "$key"; else build_one "$path"; fi
    outcome=$(cat "$run/$key.result" 2>/dev/null || :)
    set -f
    set -- $outcome
    set +f
    case "${1:-}" in
        built)
            shift 2
            if [ "$package_only" = yes ]; then
                state_path_row "$state/packaged" "$requested" "$requested $*"
                state_path_row "$state/package-failed" "$requested"
                echo "Packaged $requested from its current pkgsrc stage"
            else
                state_path_row "$state/done" "$requested" "$requested $*"
                state_path_row "$state/failed" "$requested"
                state_path_row "$state/blocked" "$requested"
                echo "Installed $requested from its current pkgsrc stage"
            fi
            [ "$stop" = 0 ] || return 143
            if [ "$role" = target ]; then shared_pair_for_path "$path"; else shared_pair=; fi
            if [ "$role" = target ] && [ -n "$shared_pair" ] && [ "$path" = "$shared_producer" ] && shared_pair_ready "$path"; then
                producer_path=$path
                partner=$shared_consumer
                key=$(echo "$partner" | tr / _)
                node=$partner
                log=$state/logs/$key.log
                reported=
                if ! shared_pair_reset_stage "$partner" >> "$log" 2>&1; then
                    if [ "$package_only" = yes ]; then
                        state_path_row "$state/package-failed" "$partner" "$partner shared-stage-cleanup"
                    else
                        state_path_row "$state/failed" "$partner" "$partner shared-stage-cleanup"
                        state_path_row "$state/blocked" "$partner" "$partner shared-stage-cleanup"
                    fi
                    echo "Could not reset the consumer package stage; retained producer WRKDIR: $producer_path" >&2
                    return 1
                fi
                [ "$stop" = 0 ] || return 143
                build_one "$partner"
                outcome=$(cat "$run/$key.result" 2>/dev/null || :)
                set -f
                set -- $outcome
                set +f
                case "${1:-}" in
                    built)
                        shift 2
                        if [ "$package_only" = yes ]; then
                            state_path_row "$state/packaged" "$partner" "$partner $*"
                            state_path_row "$state/package-failed" "$partner"
                            echo "Packaged paired package $partner from the retained $shared_pair stage"
                        else
                            state_path_row "$state/done" "$partner" "$partner $*"
                            state_path_row "$state/failed" "$partner"
                            state_path_row "$state/blocked" "$partner"
                            echo "Installed paired package $partner from the retained $shared_pair stage"
                        fi
                        ;;
                    *)
                        reason=${outcome#failed "$partner" }
                        if [ "$package_only" = yes ]; then
                            state_path_row "$state/package-failed" "$partner" "$partner ${reason:-package}"
                        else
                            state_path_row "$state/failed" "$partner" "$partner ${reason:-package-install}"
                            state_path_row "$state/blocked" "$partner"
                        fi
                        echo "Paired package stage failed; retained producer WRKDIR: $producer_path" >&2
                        return 1
                        ;;
                esac
            fi
            ;;
        done)
            echo "$requested is already recorded done"
            ;;
        *)
            case "${1:-}" in
                skipped)
                    echo "Package is skipped by the current profile: $outcome" >&2
                    return 2
                    ;;
            esac
            reason=${outcome#failed "$requested" }
            if [ "$package_only" = yes ]; then
                state_path_row "$state/package-failed" "$requested" "$requested ${reason:-package}"
            else
                state_path_row "$state/failed" "$requested" "$requested ${reason:-package-install}"
                state_path_row "$state/blocked" "$requested"
            fi
            echo "Package stage failed; retained WRKDIR: $path" >&2
            return 1
            ;;
    esac
    [ "$stop" = 0 ] || return 143
}

# Print "R PATH" for up to $1 packages whose dependencies are all done and
# "B PATH DEP" for packages with a failed, blocked or skipped dependency.
ready()
{
    awk -v max="$1" '
        FILENAME == ARGV[1] { st[$1] = $2; next }
        FILENAME == ARGV[2] { if ($1 != $2) deps[$2] = deps[$2] " " $1; next }
        { order[++n] = $1; listed[$1] = 1 }
        END {
            for (i = 1; i <= n; i++) {
                p = order[i]
                if (p in st) continue
                k = split(deps[p], d, " ")
                bad = ""; wait = 0
                for (j = 1; j <= k; j++) {
                    s = st[d[j]]
                    if (s == "failed" || s == "blocked" || s == "skipped") { bad = d[j]; break }
                    if (s != "done" && (d[j] in listed)) wait = 1
                }
                if (bad != "") { print "B", p, bad; st[p] = "blocked"; continue }
                if (!wait && started < max) { print "R", p; started++; st[p] = "running" }
            }
        }' "$run/status" "$state/edges" "$state/order"
}

build()
{
    cpu_file=$state/.cross-bulk-cpus.$$
    host_cpus > "$cpu_file" || return 1
    cpus=$(cat "$cpu_file")
    rm -f "$cpu_file"
    workers_explicit=no
    if [ -n "${CROSS_BULK_JOBS:-}" ]; then
        workers_explicit=yes
        workers=$CROSS_BULK_JOBS
    else
        workers=$(( (cpus + 1) / 2 ))
    fi
    count CROSS_BULK_JOBS "$workers"
    if [ "$workers" -gt "$cpus" ]; then
        echo "CROSS_BULK_JOBS=$workers exceeds resolved CPU capacity=$cpus" >&2
        exit 2
    fi
    if [ -n "${CROSS_BULK_MAKE_JOBS:-}" ]; then
        make_jobs=$CROSS_BULK_MAKE_JOBS
        count CROSS_BULK_MAKE_JOBS "$make_jobs"
        if [ "$make_jobs" -gt "$cpus" ]; then
            echo "CROSS_BULK_MAKE_JOBS=$make_jobs exceeds resolved CPU capacity=$cpus" >&2
            exit 2
        fi
        if [ "$workers_explicit" = yes ]; then
            max_workers=$((cpus / make_jobs))
            if [ "$workers" -gt "$max_workers" ]; then
                echo "CROSS_BULK_JOBS=$workers with CROSS_BULK_MAKE_JOBS=$make_jobs exceeds resolved CPU capacity=$cpus" >&2
                exit 2
            fi
        else
            max_workers=$((cpus / make_jobs))
            [ "$workers" -le "$max_workers" ] || workers=$max_workers
        fi
    else
        # Controller-side queries are serial; each package subshell gets its
        # own allocation before it calls build_one.
        make_jobs=1
    fi
    run=$state/run
    # Locks and results of an interrupted run; its packages are rebuilt.
    rm -rf "$run" "$state/locks" "$state/install.lock"
    mkdir "$run" "$state/locks"
    : > "$run/status"
    real_pkg_add=$(var pkgtools/pkg_install NATIVE_PKG_ADD_CMD)
    [ -n "$real_pkg_add" ] || { echo 'NATIVE_PKG_ADD_CMD is empty' >&2; exit 2; }
    pkg_add_wrapper "$real_pkg_add" > "$run/pkg_add"
    chmod +x "$run/pkg_add"
    native_info
    target_pkg_info=$(var pkgtools/pkg_install PKG_INFO)
    [ -n "$target_pkg_info" ] || { echo 'The target PKG_INFO is empty' >&2; exit 2; }
    target_settings=$(var pkgtools/pkg_install CROSSTARGETSETTINGS)
    stamp=$(date '+%Y%m%d%H%M%S')
    vars_cache=
    if [ "${CROSS_BULK_VARS_CACHE:-yes}" != no ]; then
        vars_cache=$state/vars
        mkdir -p "$vars_cache"
        gnu_stat=no
        ! stat -c %i / > /dev/null 2>&1 || gnu_stat=yes
        build_env_identity=$(environment_identity) || build_env_identity=
        if [ -n "$build_env_identity" ]; then
            build_vars_context="build|$BMAKE|$MAKECONF|$PKGSRCDIR|$native|$build_env_identity"
            vars_context=$build_vars_context
        else
            # An incomplete environment identity cannot safely reuse answers.
            vars_cache=
        fi
    fi
    shared_pairs_configure
    trash_init
    deleter_pid=
    job=0
    # The native nodes that a target package needs as a tool.
    awk '$1 ~ /@native$/ && $2 !~ /@native$/ { print $1 }' "$state/edges" | sort -u > "$run/crossfed"
    # Keep the results of an earlier run, but try its failures again.
    for list_name in failed blocked; do
        [ -s "$state/$list_name" ] && mv "$state/$list_name" "$state/$list_name.$stamp"
    done
    touch "$state/done" "$state/failed" "$state/blocked" "$state/skipped"
    packages=$(var pkgtools/pkg_install PACKAGES)/All
    native_packages=${packages%/All}-native
    link_launcher_args
    if [ "$stage_supervised" = no ]; then
        stage_supervise build "$state/order"
        return $?
    fi
    if [ -n "$trash" ]; then
        deleter $$ < /dev/null > /dev/null 2>&1 &
        deleter_pid=$!
    fi
    echo "Building with $workers packages at once (selected SDK CPU capacity=$cpus)"
    if [ -n "${CROSS_BULK_MAKE_JOBS:-}" ]; then
        echo "Explicit MAKE_JOBS=$make_jobs per package"
    else
        echo "Default MAKE_JOBS shares the $cpus CPU budget across ready packages"
    fi
    [ -z "$link_args" ] || echo "Heavy links through $CROSS_BULK_LINK_LAUNCHER"
    stop=0
    trap 'stop=1' TERM INT HUP
    running=0
    jobs_in_use=0
    changed=1
    poll_delay=0.05
    while :; do
        for res in "$run"/*.result; do
            [ -f "$res" ] || continue
            set -f
            set -- $(sed -n 1p "$res")
            set +f
            what=$1 path=$2
            shift 2
            case "$what" in
                built) echo "$path $*" >> "$state/done"; echo "$path done" >> "$run/status" ;;
                done) echo "$path done" >> "$run/status" ;;
                skipped) echo "$path $*" >> "$state/skipped"; echo "$path skipped" >> "$run/status" ;;
                *) echo "$path $*" >> "$state/failed"; echo "$path failed" >> "$run/status" ;;
            esac
            # A request of a job that ended without its replace.
            res=${res%.result}
            res_key=${res##*/}
            if [ -f "$run/$res_key.jobs" ]; then
                completed_jobs=$(cat "$run/$res_key.jobs")
                jobs_in_use=$((jobs_in_use - completed_jobs))
                rm -f "$run/$res_key.jobs"
            fi
            rm -f "$res.result" "$res.replace" "$res.replacing"
            running=$((running - 1))
            changed=1
            poll_delay=0.05
        done
        # Give one waiting job its replace when every running job waits
        # (see exclusive); start no job while one waits or replaces.
        replacing=0 waiting=0 first=
        for req in "$run"/*.replacing; do
            [ ! -f "$req" ] || replacing=1
        done
        for req in "$run"/*.replace; do
            [ -f "$req" ] || continue
            waiting=$((waiting + 1))
            [ -n "$first" ] || first=$req
        done
        if [ "$replacing" = 0 ] && [ "$waiting" -gt 0 ] && [ "$waiting" -ge "$running" ]; then
            mv "$first" "${first%.replace}.replacing"
            replacing=1 waiting=$((waiting - 1))
            poll_delay=0.05
        fi
        if [ "$stop" = 1 ]; then
            [ "$running" -gt 0 ] || {
                echo 'Stopped; the running packages finished' >&2
                trap - TERM INT HUP
                stop_deleter
                wait || :
                state_lock_release_safe=yes
                return 143
            }
        elif [ "$changed" = 1 ] && [ "$running" -lt "$workers" ] && [ "$replacing" = 0 ] && [ "$waiting" = 0 ]; then
            changed=0
            ready $((workers - running)) > "$run/ready"
            ready_left=$(awk '$1 == "R" { n++ } END { print n + 0 }' "$run/ready")
            while read -r kind path dep; do
                if [ "$kind" = B ]; then
                    echo "$path $dep" >> "$state/blocked"
                    echo "$path blocked" >> "$run/status"
                    changed=1
                    poll_delay=0.05
                else
                    worker_make_jobs=$make_jobs
                    if [ -z "${CROSS_BULK_MAKE_JOBS:-}" ]; then
                        available=$((cpus - jobs_in_use))
                        if [ "$available" -le 0 ]; then
                            ready_left=$((ready_left - 1))
                            continue
                        fi
                        slots=$((workers - running))
                        [ "$ready_left" -lt "$slots" ] && slots=$ready_left
                        [ "$available" -lt "$slots" ] && slots=$available
                        worker_make_jobs=$(((available + slots - 1) / slots))
                    fi
                    echo "$path running" >> "$run/status"
                    running=$((running + 1))
                    job=$((job + 1))
                    budget_key=$(echo "$path" | tr / _)
                    printf '%s\n' "$worker_make_jobs" > "$run/$budget_key.jobs"
                    jobs_in_use=$((jobs_in_use + worker_make_jobs))
                    ready_left=$((ready_left - 1))
                    # Without errexit: every package reports a result.
                    # The scheduler may take the result at once, so the
                    # check is a flag, not the file; build_one changes $path.
                    state_lock_release_safe=no
                    (
                        set +e
                        make_jobs=$worker_make_jobs
                        node=$path
                        reported=
                        build_one "$node"
                        [ -n "$reported" ] || result "$(echo "$node" | tr / _)" "failed $node build"
                    ) < /dev/null > /dev/null 2>&1 &
                    poll_delay=0.05
                fi
            done < "$run/ready"
            [ "$running" -gt 0 ] || [ "$changed" = 1 ] || break
        fi
        [ "$running" -gt 0 ] || [ "$changed" = 1 ] || break
        # Poll promptly after progress, then back off while workers keep running.
        sleep "$poll_delay"
        case "$poll_delay" in
            0.05) poll_delay=0.1 ;;
            0.1) poll_delay=0.2 ;;
            0.2) poll_delay=0.4 ;;
            0.4) poll_delay=0.8 ;;
            *) poll_delay=1 ;;
        esac
    done
    trap - TERM INT HUP
    stop_deleter
    wait || :
    state_lock_release_safe=yes
    failed=no
    [ ! -s "$state/failed" ] || failed=yes
    [ ! -s "$state/blocked" ] || failed=yes
    rm -rf "$run" "$state/locks"
    [ "$failed" = no ] || return 1
    return 0
}

# The deleter stops with the build (a tree it is removing is finished);
# the next build removes the rest.
stop_deleter()
{
    [ -n "$deleter_pid" ] || return 0
    kill "$deleter_pid" 2> /dev/null || :
    wait "$deleter_pid" 2> /dev/null || :
    deleter_pid=
}

case "$mode" in
    order) order ;;
    selected-order) selected_order ;;
    selected-build) selected_build ;;
    deps)
        native_info
        case "$list" in
            *'|'*) key=${list%%\|*}; path=${list#*\|} ;;
            *) key=$(echo "$list" | tr / _); path=$list ;;
        esac
        if [ -n "${CROSS_BULK_VARS_CONTEXT:-}" ] &&
            [ "${CROSS_BULK_VARS_CACHE:-yes}" != no ]; then
            vars_cache=$(dirname "$state")/vars
            mkdir -p "$vars_cache"
            case "$path" in *@native) vars_context="$CROSS_BULK_VARS_CONTEXT|native" ;; *) vars_context="$CROSS_BULK_VARS_CONTEXT|target" ;; esac
        fi
        node_deps "$path" > "$state/$key.tmp" 2> "$state/$key.err" &&
            mv "$state/$key.tmp" "$state/$key.deps" ;;
    fetch) fetch ;;
    build) build ;;
    package|package-install|patch-refresh|buildlink-refresh) package_install_one ;;
    *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
