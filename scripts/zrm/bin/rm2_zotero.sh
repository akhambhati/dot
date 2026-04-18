#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/config/config.env"

mkdir -p "$ROOT/state" "$ROOT/logs" "$ROOT/exports" "$ZOTERO_ANNOTATED_DIR"

VERBOSE_FLAG=""
if [[ "${VERBOSE:-0}" == "1" ]]; then
  VERBOSE_FLAG="--verbose"
fi

run_inspect_zotero() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG inspect-zotero \
    --zotero-db "$ZOTERO_DB" \
    --zotero-storage-dir "$ZOTERO_STORAGE_DIR" \
    --collection-name "$COLLECTION_NAME"
}

run_reconcile() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG reconcile \
    --zotero-db "$ZOTERO_DB" \
    --zotero-storage-dir "$ZOTERO_STORAGE_DIR" \
    --xochitl-dir "$XOCHITL_DIR" \
    --state-db "$STATE_DB"
}

run_push() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG push \
    --zotero-db "$ZOTERO_DB" \
    --zotero-storage-dir "$ZOTERO_STORAGE_DIR" \
    --collection-name "$COLLECTION_NAME" \
    --xochitl-dir "$XOCHITL_DIR" \
    --rm-parent-name "$RM_PARENT_NAME" \
    --state-db "$STATE_DB"
}

run_pull() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG pull \
    --xochitl-dir "$XOCHITL_DIR" \
    --annotated-dir "$ZOTERO_ANNOTATED_DIR" \
    --remarks-bin "$REMARKS_BIN" \
    --state-db "$STATE_DB"
}

run_attach() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG attach \
    --annotated-dir "$ZOTERO_ANNOTATED_DIR" \
    --state-db "$STATE_DB" \
    --zotero-library-id "$ZOTERO_LIBRARY_ID" \
    --zotero-library-type "$ZOTERO_LIBRARY_TYPE" \
    --zotero-api-key "$ZOTERO_API_KEY"
}

request_xochitl_restart() {
  [[ "${RM2_REQUEST_XOCHITL_RESTART:-0}" == "1" ]] || return 0

  local queue_dir="$RM2_CONTROL_DIR/queue"
  mkdir -p "$queue_dir"

  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local req="$queue_dir/restart-xochitl-${stamp}.request"

  : > "$req"
  echo "Requested RM2 xochitl restart: $req"
}

run_push_and_request_restart() {
  run_push
  request_xochitl_restart
}

run_sync() {
  "$PYTHON_BIN" "$ROOT/bin/rm2_zotero.py" $VERBOSE_FLAG sync \
    --zotero-db "$ZOTERO_DB" \
    --zotero-storage-dir "$ZOTERO_STORAGE_DIR" \
    --collection-name "$COLLECTION_NAME" \
    --xochitl-dir "$XOCHITL_DIR" \
    --rm-parent-name "$RM_PARENT_NAME" \
    --annotated-dir "$ZOTERO_ANNOTATED_DIR" \
    --remarks-bin "$REMARKS_BIN" \
    --state-db "$STATE_DB" \
    --zotero-library-id "$ZOTERO_LIBRARY_ID" \
    --zotero-library-type "$ZOTERO_LIBRARY_TYPE" \
    --zotero-api-key "$ZOTERO_API_KEY"

  request_xochitl_restart
}

case "${1:-}" in
  inspect-zotero)
    run_inspect_zotero
    ;;
  reconcile)
    run_reconcile
    ;;
  push)
    run_push_and_request_restart
    ;;
  pull)
    run_pull
    ;;
  attach)
    run_attach
    ;;
  sync)
    run_sync
    ;;
  once)
    run_sync
    ;;
  watch)
    while true; do
      run_sync || true
      sleep "${POLL_SECONDS:-60}"
    done
    ;;
  *)
    echo "Usage: $0 {inspect-zotero|reconcile|push|pull|attach|sync|once|watch}"
    exit 1
    ;;
esac
