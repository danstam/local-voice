#!/bin/sh
set -eu
project=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
case "$(uname -s)" in Darwin) ;; *) echo 'Local Voice currently supports macOS and Windows.' >&2; exit 1 ;; esac
pointer="$project/.mac-runtime/active-runtime.txt"
if [ ! -f "$pointer" ]; then echo 'Run ./setup.cmd first. No private runtime is installed.' >&2; exit 1; fi
name=$(cat "$pointer")
case "$name" in ''|*[!a-z0-9-]*) echo 'Invalid private runtime selection.' >&2; exit 1 ;; esac
python="$project/.mac-runtime/runtimes/$name/python/bin/python3"
if [ ! -x "$python" ]; then echo 'The private runtime needs repair. Rerun ./setup.cmd.' >&2; exit 1; fi
exec "$python" -I -X utf8 -u "$project/windows_entry.py" "$@"
