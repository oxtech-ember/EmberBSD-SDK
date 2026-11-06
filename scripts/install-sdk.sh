#!/bin/sh
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# First acquisition uses macOS services; later operations use the installed SDK.
set -eu
LC_ALL=C; export LC_ALL

fail() { echo "install-sdk: $*" >&2; exit 1; }
[ "$#" -eq 1 ] || fail "usage: sh install-sdk.sh /absolute/new/sdk-directory"
case "$1" in /*) destination=$1 ;; *) fail "select an absolute SDK directory" ;; esac
case "$destination" in *[!A-Za-z0-9_./-]*) fail "Ports requires a path without spaces or shell metacharacters" ;; esac
[ ! -e "$destination" ] && [ ! -L "$destination" ] || fail "destination already exists; use its ember-sdk client"
[ "$(/usr/bin/uname -s)/$(/usr/bin/uname -m)" = Darwin/arm64 ] || fail "this bootstrap currently supports macOS arm64"
major=$(/usr/bin/sw_vers -productVersion); major=${major%%.*}
[ "$major" -ge 27 ] || fail "this published bootstrap requires macOS 27 or newer"
parent=${destination%/*}; [ -n "$parent" ] || parent=/
[ -d "$parent" ] || fail "destination parent does not exist"
parent=$(CDPATH= cd -- "$parent" && pwd -P)
destination=$parent/${destination##*/}
temporary=$(/usr/bin/mktemp -d "$parent/.ember-install.XXXXXX")
trap '/bin/rm -rf "$temporary"' EXIT HUP INT TERM
base=https://emberbsd-pkg.oxteam.me
get() {
    /usr/bin/curl --fail --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 600 --retry 3 --retry-delay 2 \
        --retry-max-time 900 --silent --show-error "$1" -o "$2"
}
field() { /usr/bin/plutil -extract "$2" raw -o - "$1"; }
verify() {
    /usr/bin/openssl cms -verify -binary -inform DER -in "$1" -CAfile "$temporary/root.pem" \
        -CApath "$temporary/empty-ca" -purpose smimesign -verify_depth 1 -x509_strict -check_ss_sig \
        -signer "$temporary/signer.pem" -out "$2" 2>"$temporary/verify.log" || {
        /bin/cat "$temporary/verify.log" >&2; fail "release signature verification failed";
    }
    [ "$(/usr/bin/grep -c '^-----BEGIN CERTIFICATE-----$' "$temporary/signer.pem")" = 1 ] || fail "expected one release signer"
    /usr/bin/openssl x509 -in "$temporary/signer.pem" -text -noout > "$temporary/signer.txt"
    # LibreSSL has no auth_level switch. The initial RSA-3072/SHA256 profile
    # meets the client's level-2 requirement without allowing a weaker signer.
    /usr/bin/grep -q 'Public-Key: (3072 bit)' "$temporary/signer.txt" || fail "unsupported release key profile"
    /usr/bin/grep -q 'Signature Algorithm: sha256WithRSAEncryption' "$temporary/signer.txt" || fail "unsupported release certificate signature"
    profile=$(/usr/bin/awk '
      /X509v3 Basic Constraints:/ { getline; gsub(/^[ \t]+|[ \t]+$/, ""); print "BC=" $0 }
      /X509v3 Key Usage:/ { getline; gsub(/^[ \t]+|[ \t]+$/, ""); print "KU=" $0 }
      /X509v3 Extended Key Usage:/ { getline; gsub(/^[ \t]+|[ \t]+$/, ""); print "EKU=" $0 }
    ' "$temporary/signer.txt" | /usr/bin/sort)
    [ "$profile" = 'BC=CA:FALSE
EKU=Code Signing, E-mail Protection
KU=Digital Signature' ] || fail "certificate is not an EmberBSD release signer"
}
check_file() {
    [ "$(/usr/bin/stat -f %z "$1")" = "$2" ] || fail "download size differs from the signed manifest"
    actual=$(/usr/bin/shasum -a 256 "$1"); actual=${actual%% *}
    [ "$actual" = "$3" ] || fail "download checksum differs from the signed manifest"
}

# The fingerprint pins the repository's public signing root before execution.
get https://raw.githubusercontent.com/oxtech-ember/EmberBSD-SDK/main/trust/emberbsd-release-root.pem "$temporary/root.pem"
fingerprint=$(/usr/bin/openssl x509 -in "$temporary/root.pem" -noout -fingerprint -sha256)
[ "${fingerprint#*=}" = D5:BF:B3:64:48:44:A5:3E:B8:37:92:98:52:8C:B4:9C:4A:81:0B:E3:B4:B7:E2:6B:54:2A:BA:72:BE:46:1B:0C ] || fail "unexpected EmberBSD trust root"
/bin/mkdir "$temporary/empty-ca"
get "$base/current.p7m" "$temporary/current.p7m"
verify "$temporary/current.p7m" "$temporary/current.json"
[ "$(field "$temporary/current.json" schema)" = emberbsd.channel.v2 ] || fail "unsupported current format"
[ "$(field "$temporary/current.json" channel)" = current ] || fail "unexpected channel"
now=$(/bin/date +%s)
[ "$(field "$temporary/current.json" issued_at)" -le "$now" ] && \
    [ "$now" -lt "$(field "$temporary/current.json" expires_at)" ] || fail "current is not valid at this time"
release=$(field "$temporary/current.json" release.path)
case "$release" in current/metadata/*/release.p7m) ;; *) fail "invalid release path" ;; esac
get "$base/$release" "$temporary/release.p7m"
check_file "$temporary/release.p7m" "$(field "$temporary/current.json" release.size)" "$(field "$temporary/current.json" release.sha256)"
verify "$temporary/release.p7m" "$temporary/release.json"
[ "$(field "$temporary/release.json" schema)" = emberbsd.release.v1 ] || fail "unsupported release format"
[ "$(field "$temporary/release.json" release_id)" = "$(field "$temporary/current.json" release.id)" ] || fail "release identity differs"
index=0; selected=
while kind=$(field "$temporary/release.json" "artifacts.$index.kind" 2>/dev/null); do
    if [ "$kind" = bootstrap ] && \
        [ "$(field "$temporary/release.json" "artifacts.$index.host.os")" = Darwin ] && \
        [ "$(field "$temporary/release.json" "artifacts.$index.host.arch")" = arm64 ] && \
        [ "$(field "$temporary/release.json" "artifacts.$index.host.abi")" = macos-27.0 ] && \
        [ "$(field "$temporary/release.json" "artifacts.$index.host.runtime")" = libSystem ]; then
        path=$(field "$temporary/release.json" "artifacts.$index.path")
        case "$path" in *.tar.gz) [ -z "$selected" ] || fail "ambiguous bootstrap selection"; selected=$index ;; esac
    fi
    index=$((index + 1))
done
[ -n "$selected" ] || fail "current has no self-contained bootstrap for this host"
path=$(field "$temporary/release.json" "artifacts.$selected.path")
case "$path" in /*|*../*) fail "invalid bootstrap path" ;; esac
get "$base/current/host/darwin-arm64/${path##*/}" "$temporary/sdk.tar.gz"
check_file "$temporary/sdk.tar.gz" "$(field "$temporary/release.json" "artifacts.$selected.size")" "$(field "$temporary/release.json" "artifacts.$selected.sha256")"
/bin/mkdir "$temporary/unpack"
/usr/bin/tar -xzf "$temporary/sdk.tar.gz" -C "$temporary/unpack"
[ -x "$temporary/unpack/ember-sdk/bin/ember-sdk" ] || fail "bootstrap has no SDK launcher"
/bin/mv "$temporary/unpack/ember-sdk" "$destination"
"$destination/bin/ember-sdk" --help >/dev/null
echo "SDK installed: $destination/bin/ember-sdk"
echo "List components: $destination/bin/ember-sdk list"
