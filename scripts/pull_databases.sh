#!/usr/bin/env bash
# Pull the derived SQLite databases needed to SERVE the site.
# Reads locations from datasets/manifests/db_snapshots.json.
set -euo pipefail
SRC_HOST="${OSTI_DB_HOST:-100.86.220.115}"          # m1 tailnet
SRC_DIR="${OSTI_DB_DIR:-/Users/stevens/code/osti-cards-site/data}"
DEST="${1:-web/data}"
mkdir -p "$DEST"
for db in osti_cards.sqlite supercards.sqlite entities.sqlite citations.sqlite card_enrichment.sqlite; do
  echo "pulling $db ..."
  rsync -av --partial "stevens@${SRC_HOST}:${SRC_DIR}/${db}" "$DEST/${db}"
done
echo "done -> $DEST"
echo "verify small-DB sha256 against datasets/manifests/db_snapshots.json"
