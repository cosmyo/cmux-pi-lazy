#!/bin/sh
# Restore the previous launcher, but never clobber a subsequently changed pi.
set -eu
bin="$HOME/.local/bin"
data="$HOME/.local/share/pi-lazy"
if [ ! -L "$bin/pi" ] || [ "$(readlink "$bin/pi")" != 'pi-lazy' ]; then
    printf 'pi is no longer our symlink; leaving it untouched.\n' >&2
    exit 1
fi
if [ ! -f "$data/original-pi-backup" ]; then
    printf 'Original launcher backup record is missing; refusing to guess.\n' >&2
    exit 1
fi
IFS= read -r backup < "$data/original-pi-backup"
if [ "$backup" = '-' ]; then
    rm "$bin/pi"
elif [ -e "$backup" ] || [ -L "$backup" ]; then
    rm "$bin/pi"
    mv "$backup" "$bin/pi"
else
    printf 'Original launcher backup is missing; leaving pi untouched.\n' >&2
    exit 1
fi
rm "$data/original-pi-backup"
printf 'Previous pi launcher restored. Existing processes and private title cache were not touched.\n'
printf 'Inactive launcher files remain under %s for inspection.\n' "$data"
