#!/usr/bin/env bash
# Spill pathlog/ticks into log_archive/ when old or oversized.
#
# Local-only: no Drive credentials. A later Drive mirror can pick up
# log_archive/*.tgz the same way other bot log archives are mirrored.
#
# Usage:
#   scripts/archive_pathlog_ticks.sh
#   scripts/archive_pathlog_ticks.sh --days 7 --max-mb 200
#   scripts/archive_pathlog_ticks.sh --dry-run
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TICK_DIR="${REPO}/pathlog/ticks"
OUT_DIR="${REPO}/log_archive"
DAYS=14
MAX_MB=300
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="${2:?}"; shift 2 ;;
    --max-mb) MAX_MB="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '1,20p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT_DIR"
if [[ ! -d "$TICK_DIR" ]]; then
  echo "missing ticks dir: $TICK_DIR" >&2
  exit 1
fi

now_epoch="$(date +%s)"
cutoff=$(( now_epoch - DAYS * 86400 ))
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp_list="$(mktemp)"
trap 'rm -f "$tmp_list"' EXIT

# Collect candidates older than DAYS (by mtime).
find "$TICK_DIR" -maxdepth 1 -type f -name '*.jsonl' -printf '%T@ %p\n' \
  | while read -r mtime path; do
      mtime_i="${mtime%.*}"
      if (( mtime_i < cutoff )); then
        echo "$path"
      fi
    done > "$tmp_list"

bytes="$(du -sb "$TICK_DIR" 2>/dev/null | awk '{print $1}')"
bytes="${bytes:-0}"
max_bytes=$(( MAX_MB * 1024 * 1024 ))

# If still over MAX_MB after age select, add oldest remaining until under cap
# (or until we have something to archive).
if (( bytes > max_bytes )); then
  # Recompute: age files already listed; add more oldest-first.
  find "$TICK_DIR" -maxdepth 1 -type f -name '*.jsonl' -printf '%T@ %s %p\n' \
    | sort -n \
    | while read -r mtime size path; do
        echo "$path"
      done > "${tmp_list}.all"
  : > "$tmp_list"
  running="$bytes"
  protect_recent=120
  while read -r path; do
    [[ -f "$path" ]] || continue
    mtime_i="$(stat -c %Y "$path")"
    if (( now_epoch - mtime_i < protect_recent )); then
      continue
    fi
    echo "$path" >> "$tmp_list"
    sz="$(stat -c %s "$path")"
    running=$(( running - sz ))
    if (( running <= max_bytes )); then
      # Keep going for age-expired too — already included if older.
      :
    fi
    # Stop once under cap AND we have at least the age set; for size spill
    # stop when under cap.
    if (( running <= max_bytes )); then
      break
    fi
  done < "${tmp_list}.all"
  rm -f "${tmp_list}.all"
fi

# Also ensure age-expired are present even if already under size cap.
find "$TICK_DIR" -maxdepth 1 -type f -name '*.jsonl' -printf '%T@ %p\n' \
  | while read -r mtime path; do
      mtime_i="${mtime%.*}"
      if (( mtime_i < cutoff )); then
        grep -Fxq "$path" "$tmp_list" 2>/dev/null || echo "$path" >> "$tmp_list"
      fi
    done

count="$(grep -c . "$tmp_list" 2>/dev/null || true)"
count="${count:-0}"
if [[ "$count" -eq 0 ]]; then
  echo "nothing to archive (days=${DAYS} max_mb=${MAX_MB} ticks_bytes=${bytes})"
  exit 0
fi

out_tgz="${OUT_DIR}/pathlog_ticks_${stamp}.tgz"
echo "archiving ${count} files → ${out_tgz} (ticks_bytes_before=${bytes})"
if [[ "$DRY_RUN" -eq 1 ]]; then
  head -20 "$tmp_list"
  [[ "$count" -gt 20 ]] && echo "… $((count - 20)) more"
  echo "dry-run: would write ${out_tgz}"
  exit 0
fi

# tar from repo so paths are pathlog/ticks/...
cd "$REPO"
tar -czf "$out_tgz" -T <(sed "s|^${REPO}/||" "$tmp_list")
# Remove only after successful tgz.
while read -r path; do
  rm -f "$path"
done < "$tmp_list"

echo "wrote ${out_tgz}"
ls -lh "$out_tgz"
# Drive can mirror log_archive/ later (same routine as other bot log spills).
