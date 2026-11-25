#!/bin/bash
# Move Project Gautam's data between your Mac and Isambard-AI. Code travels via git; this moves the rest.
#
#   bash isambard/sync.sh status          what is on Isambard, and your running jobs
#   bash isambard/sync.sh push-pdfs [dir] send PDFs (default: ./articles) to Isambard's pdfs/
#   bash isambard/sync.sh logs            fetch job logs (gautam-*.out) and eval reports
#   bash isambard/sync.sh pull            fetch the library built on Isambard and make it your Mac's library
#
# Uses $ISAMBARDAI (address of Isambard). Optional: ISAMBARD_USER (default scdantu.u6wf),
# ISAMBARD_DIR (default /projects/u6wf/scdantu/Projects/gautam).
set -euo pipefail

G="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # this gautam folder on the Mac
: "${ISAMBARDAI:?Set ISAMBARDAI to the address of Isambard first, e.g. export ISAMBARDAI=1.2.3.4}"
USER_AT="${ISAMBARD_USER:-scdantu.u6wf}"
case "$ISAMBARDAI" in *@*) HOST="$ISAMBARDAI" ;; *) HOST="$USER_AT@$ISAMBARDAI" ;; esac
RDIR="${ISAMBARD_DIR:-/projects/u6wf/scdantu/Projects/gautam}"
RS=(rsync -az --partial --info=progress2)
rsync --version 2>/dev/null | grep -q "version 3" || RS=(rsync -az --partial --progress)  # macOS's old rsync

cmd="${1:-help}"
case "$cmd" in
status)
  ssh "$HOST" "cd '$RDIR' && echo '== library on Isambard ==' && ls -lh data_isambard/gautam.sqlite 2>/dev/null || echo '(none yet)';
               echo; echo '== downloads ==' && du -sh harvest_xml pdfs 2>/dev/null;
               echo; echo '== your jobs ==' && squeue --me;
               echo; echo '== latest log (last 5 lines) ==' && tail -n 5 \$(ls -t gautam-*.out 2>/dev/null | head -1) 2>/dev/null"
  ;;

push-pdfs)
  SRC="${2:-$G/articles}"
  [ -d "$SRC" ] || { echo "No folder $SRC"; exit 1; }
  echo "Sending PDFs from $SRC to $HOST:$RDIR/pdfs/"
  "${RS[@]}" --include='*/' --include='*.pdf' --include='*.PDF' --exclude='*' "$SRC/" "$HOST:$RDIR/pdfs/"
  ;;

logs)
  mkdir -p "$G/isambard_logs"
  "${RS[@]}" "$HOST:$RDIR/gautam-*.out" "$G/isambard_logs/" 2>/dev/null || true
  "${RS[@]}" "$HOST:$RDIR/data_isambard/reports/" "$G/isambard_logs/reports/" 2>/dev/null || true
  echo "Logs and reports in $G/isambard_logs/"
  ;;

pull)
  # 1. Claude Desktop keeps the library open; swapping it underneath would confuse it.
  if pgrep -f gautam-mcp >/dev/null; then
    echo "Claude Desktop is using the library. Quit it (Cmd-Q), then run this again."; exit 1
  fi
  # 2. Is a job still writing to it?
  if ssh "$HOST" "squeue --me -h -n gautam | grep -q ."; then
    echo "A 'gautam' job is still running on Isambard; wait for it to finish (bash isambard/sync.sh status)."; exit 1
  fi
  # 3. Fetch into data_new/ (the current library stays untouched until the copy is checked)
  mkdir -p "$G/data_new"
  # Seed with the current library so rsync only sends the parts that changed (APFS clone: instant, no extra space)
  if [ -f "$G/data/gautam.sqlite" ] && [ ! -f "$G/data_new/gautam.sqlite" ]; then
    cp -c "$G/data/gautam.sqlite" "$G/data_new/gautam.sqlite" 2>/dev/null || cp "$G/data/gautam.sqlite" "$G/data_new/gautam.sqlite"
  fi
  echo "Fetching the library (only changed parts are sent)..."
  "${RS[@]}" "$HOST:$RDIR/data_isambard/gautam.sqlite" "$G/data_new/"
  "${RS[@]}" "$HOST:$RDIR/data_isambard/reports/" "$G/data_new/reports/" 2>/dev/null || true
  # 4. Check the copy is a healthy database before using it
  python3 - "$G/data_new/gautam.sqlite" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
ok = db.execute("PRAGMA quick_check").fetchone()[0]
p, c = db.execute("SELECT count(*) FROM papers").fetchone()[0], db.execute("SELECT count(*) FROM chunks").fetchone()[0]
w = db.execute("SELECT count(*) FROM chunks WHERE emb IS NULL").fetchone()[0]
print(f"Check: {ok}. {p} papers, {c} passages, {w} without fingerprints.")
sys.exit(0 if ok == "ok" else 1)
EOF
  # 5. Swap: keep the old library as a dated backup, carry over your summaries and exported .bib files
  STAMP=$(date +%Y%m%d-%H%M)
  if [ -d "$G/data" ]; then
    for sub in summaries bib; do
      [ -d "$G/data/$sub" ] && mkdir -p "$G/data_new/$sub" && cp -n "$G/data/$sub/"* "$G/data_new/$sub/" 2>/dev/null || true
    done
    mv "$G/data" "$G/data_old_$STAMP"
  fi
  mv "$G/data_new" "$G/data"
  echo "Done. New library in data/. Previous one kept as data_old_$STAMP (delete it when you're happy)."
  echo "Next: run 'gautam index' once, so your Mac's PDFs are re-linked (nothing is re-read),"
  echo "      then restart Claude Desktop. The first search builds a cache and takes ~1 minute."
  ;;

*)
  sed -n '2,10p' "$0"
  ;;
esac
