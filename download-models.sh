#!/usr/bin/env bash
# Download GGUF models listed in models.csv into models/.
#
#   ./download-models.sh                 # the default model only
#   ./download-models.sh gemma-3-4b      # one named model
#   ./download-models.sh gemma-3-4b llama-3.1-8b
#   ./download-models.sh --all           # everything in the catalogue
#   ./download-models.sh --list          # show the catalogue and exit
#
# Add a model by adding a line to models.csv - no change here is needed.
# Downloads resume if interrupted, and a model already present is skipped.

set -euo pipefail

cd "$(dirname "$0")"
CATALOGUE="models.csv"
mkdir -p models

[ -f "$CATALOGUE" ] || { echo "error: $CATALOGUE not found" >&2; exit 1; }

# Strip comments and blank lines once, so every reader below sees clean rows.
rows() { grep -v '^[[:space:]]*#' "$CATALOGUE" | grep -v '^[[:space:]]*$'; }

if [ "${1:-}" = "--list" ]; then
    printf '%-16s %-8s %s\n' NAME ROLE FILE
    rows | while IFS=, read -r name repo file role; do
        printf '%-16s %-8s %s\n' "$name" "$role" "$file"
    done
    exit 0
fi

# Work out which catalogue names were asked for.
wanted=""
if [ "${1:-}" = "--all" ]; then
    wanted=$(rows | cut -d, -f1)
elif [ $# -gt 0 ]; then
    wanted="$*"
else
    wanted=$(rows | awk -F, '$4 == "default" {print $1}')
    [ -n "$wanted" ] || { echo "error: no model marked 'default' in $CATALOGUE" >&2; exit 1; }
fi

status=0
for want in $wanted; do
    line=$(rows | awk -F, -v w="$want" '$1 == w {print; exit}')
    if [ -z "$line" ]; then
        echo "error: '$want' is not in $CATALOGUE (try --list)" >&2
        status=1
        continue
    fi
    IFS=, read -r name repo file role <<< "$line"

    target="models/$file"
    if [ -s "$target" ]; then
        echo "already present, skipping: $target"
        continue
    fi

    echo "downloading $name -> $target"
    # --continue-at - resumes a partial file; --fail so an HTTP error is not
    # written to disk as if it were a model. The progress meter is suppressed
    # when output is redirected, where it otherwise writes a line per update
    # and buries everything else in the log.
    progress="--progress-bar"
    [ -t 1 ] || progress="--no-progress-meter"
    if curl -L --fail --continue-at - $progress -o "$target" \
        "https://huggingface.co/$repo/resolve/main/$file"; then
        echo "done: $target"
    else
        echo "error: failed to download $name from $repo" >&2
        status=1
    fi
done

exit $status
