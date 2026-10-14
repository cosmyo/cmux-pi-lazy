#!/bin/sh
# Install without modifying cmux, Pi, or any existing terminal process.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin="$HOME/.local/bin"
data="$HOME/.local/share/pi-lazy"
real_pi="$HOME/.pi/agent/bin/pi"
if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != '--pi' ]; then
        printf 'Usage: %s [--pi /absolute/path/to/real/pi]\n' "$0" >&2
        exit 2
    fi
    real_pi="$2"
fi
case "$real_pi" in /*) ;; *) printf 'Real Pi path must be absolute.\n' >&2; exit 2 ;; esac
if [ ! -f "$real_pi" ] || [ ! -x "$real_pi" ]; then
    printf 'Pi is not executable at %s. Use --pi with its real path.\n' "$real_pi" >&2
    exit 1
fi
real_pi=$(/usr/bin/python3 - "$real_pi" "$bin" "$data" <<'PY'
from pathlib import Path
import sys
real = Path(sys.argv[1]).resolve(strict=True)
bin_dir, data_dir = Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
replaced = {bin_dir / 'pi', bin_dir / 'pi-lazy', data_dir / 'launcher.py'}
if real in replaced or real == (bin_dir / 'pi-lazy').resolve():
    raise SystemExit('Refusing a real Pi target that installation replaces or makes recursive.')
if 'cmux-cli-shims' in real.parts:
    raise SystemExit('Select the original Pi executable, not a generated cmux shim.')
print(real)
PY
)
if [ -f "$data/original-pi-backup" ] &&
   { [ ! -L "$bin/pi" ] || [ "$(readlink "$bin/pi")" != 'pi-lazy' ]; }; then
    printf 'pi changed since installation; refusing to overwrite it. Preserve it before manual recovery.\n' >&2
    exit 1
fi
if [ -e "$bin/pi-lazy" ] && ! grep -q '^# cmux-pi-lazy:' "$bin/pi-lazy"; then
    printf 'Refusing to overwrite an unrelated %s/pi-lazy.\n' "$bin" >&2
    exit 1
fi
mkdir -p "$bin" "$data"
if [ ! -f "$data/original-pi-backup" ]; then
    backup='-'
    if [ -e "$bin/pi" ] || [ -L "$bin/pi" ]; then
        backup="$bin/pi.before-cmux-lazy.$(date +%Y%m%d-%H%M%S).$$"
        cp -P "$bin/pi" "$backup"
    fi
    printf '%s\n' "$backup" > "$data/original-pi-backup"
    chmod 600 "$data/original-pi-backup"
fi
cp "$root/launcher.py" "$data/launcher.py"
cp "$root/bin/pi-lazy" "$bin/pi-lazy"
printf '%s\n' "$real_pi" > "$data/real-pi"
chmod 600 "$data/real-pi"
chmod 700 "$bin/pi-lazy"
ln -sfn pi-lazy "$bin/pi"
printf 'Installed. Keep using pi; existing Pi processes were not stopped.\n'
printf 'Ensure %s is on PATH. Real Pi: %s\n' "$bin" "$real_pi"
