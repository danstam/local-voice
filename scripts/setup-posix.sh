#!/bin/sh
set -eu
project=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
case "$(uname -s)" in
    Darwin) ;;
    *) echo 'Local Voice currently supports macOS and Windows.' >&2; exit 1 ;;
esac
if [ "${1:-}" = '--help' ]; then
    echo 'Automatic Local Voice setup: --check, --model small|turbo|medium|large-v3, --cpu, --prepare-only, --remove-command'
    exit 0
fi
major=$(sw_vers -productVersion | cut -d. -f1)
if [ "$major" -lt 14 ]; then echo 'This Mac needs macOS 14 or newer to run Local Voice. Update macOS, then retry.' >&2; exit 1; fi
architecture=$(uname -m)
case "$architecture" in arm64|x86_64) ;; *) echo 'Local Voice does not support this Mac yet.' >&2; exit 1 ;; esac
base="$project/.mac-runtime"
if ! mkdir -p "$base" 2>/dev/null; then
    echo 'Local Voice cannot write to this folder. Move the project to a folder you can edit, then retry.' >&2
    exit 1
fi
log="$base/bootstrap.log"
fail() { echo "$1" >&2; echo "Details saved to: $log" >&2; exit 1; }
for option in "$@"; do
    if [ "$option" = '--check' ] || [ "$option" = '--remove-command' ]; then
        exec /bin/sh "$project/scripts/start-posix.sh" --mac-setup "$@"
    fi
done
manifest="$project/macos/runtime.json"
url=$(/usr/bin/plutil -extract "$architecture.url" raw -o - "$manifest" 2>>"$log") || fail 'Setup files are incomplete. Download the project again, then retry.'
checksum=$(/usr/bin/plutil -extract "$architecture.sha256" raw -o - "$manifest" 2>>"$log") || fail 'Setup files are incomplete. Download the project again, then retry.'
archive="$base/python-runtime.tar.gz"
partial="$archive.part"
valid() { [ -f "$1" ] && [ "$(/usr/bin/shasum -a 256 "$1" 2>>"$log" | cut -d' ' -f1)" = "$checksum" ]; }
if ! valid "$archive"; then
    echo 'Downloading files Local Voice needs... this may take a few minutes.'
    if ! valid "$partial"; then
        /usr/bin/curl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 20 --max-time 900 --continue-at - --output "$partial" "$url" 2>>"$log" || {
            result=$?
            case "$result" in
                22|33) rm -f "$partial"; /usr/bin/curl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 20 --max-time 900 --output "$partial" "$url" 2>>"$log" || fail 'The download could not finish. Check your internet connection, then run ./setup.cmd again.' ;;
                *) fail 'The download could not finish. Check your internet connection and free disk space, then run ./setup.cmd again.' ;;
            esac
        }
    fi
    if ! valid "$partial"; then rm -f "$partial"; fail 'Downloaded files could not be verified. Run ./setup.cmd again to retry.'; fi
    mv -f "$partial" "$archive"
fi
bootstrap=$(mktemp -d "$base/bootstrap-XXXXXXXX")
trap 'rm -rf "$bootstrap"' EXIT HUP INT TERM
/usr/bin/tar -xzf "$archive" -C "$bootstrap" 2>>"$log" || fail 'Setup could not unpack its files. Check free disk space, then run ./setup.cmd again.'
"$bootstrap/python/bin/python3" -I -X utf8 -u "$project/windows_entry.py" --mac-setup --bootstrap "$bootstrap" "$@"
