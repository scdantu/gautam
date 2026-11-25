#!/bin/bash
#SBATCH --job-name=gautam
#SBATCH --gpus=1
#SBATCH --time=24:00:00
#SBATCH --output=gautam-graph-%j.out
# Grow the library along its citation graph, heaviest papers first.
#   GRAPH_DRY=1 sbatch isambard/graph.sbatch   -> read references, score, show the top 50, add nothing
#   sbatch isambard/graph.sbatch               -> up to GRAPH_ROUNDS rounds (default 3), then fetch + fingerprint
# Settings: [graph] in isambard/gautam.isambard.toml (`gautam graph settings` lists them).
set -uo pipefail
G="${SLURM_SUBMIT_DIR:-$PWD}"
cd "$G" || exit 1
[ -f isambard/setup.sh ] || { echo "Submit this from your gautam folder (cd there first)."; exit 1; }
export HF_HOME="$G/.hf" HF_HUB_OFFLINE=1 GAUTAM_NO_WAL=1 GAUTAM_CONFIG="$G/isambard/gautam.isambard.toml"
source "$G/.venv-isambard/bin/activate"
python -c "import torch; assert torch.cuda.is_available(), 'No GPU visible'; print('GPU:', torch.cuda.get_device_name(0))" || exit 1
ok() { curl -s -o /dev/null -m 30 -w "%{http_code}" "$1" | grep -q "^2"; }
ok "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=protein&format=json&pageSize=1" \
  || { echo "Europe PMC not reachable from this node"; exit 1; }
XREF=""; ok "https://api.crossref.org/works?rows=0" || XREF="--no-crossref"
XML=""; [ -d harvest_xml ] && XML="--xml harvest_xml"

echo "=== 1. reference lists (from downloaded XML, then online) ==="
gautam graph refs $XML $XREF
echo "=== starting coverage ==="
gautam graph coverage

if [ "${GRAPH_DRY:-0}" = 1 ]; then
  echo "=== dry run: score and show the 50 heaviest missing papers (nothing added) ==="
  gautam graph score
  gautam graph top -n 50
  exit 0
fi

echo "=== 2. weighted rounds ==="
gautam graph round --rounds "${GRAPH_ROUNDS:-3}" $XREF
gautam graph top -n 50
echo "=== 3. fetch open-access full text for added papers, then fingerprint ==="
gautam harvest download --to harvest_xml
gautam harvest ingest --from harvest_xml
gautam graph refs --xml harvest_xml $XREF
gautam index --stage embed
gautam graph coverage
gautam status
[ -f tests/eval_pduarticles.json ] && { echo "=== search quality check ==="; gautam eval; }
