#!/bin/bash
#SBATCH --job-name=gautam
#SBATCH --gpus=1
#SBATCH --time=12:00:00
#SBATCH --output=gautam-abstracts-%j.out
# Add open-access reviews (full text), then title + abstract for EVERY paper on your topics (all years, open access or not), then fingerprint.
# Resumable: re-submit if it stops; papers already stored are skipped.
set -uo pipefail
G="${SLURM_SUBMIT_DIR:-$PWD}"
cd "$G" || exit 1
[ -f isambard/setup.sh ] || { echo "Submit this from your gautam folder (cd there first)."; exit 1; }
export HF_HOME="$G/.hf" HF_HUB_OFFLINE=1 GAUTAM_NO_WAL=1 GAUTAM_CONFIG="$G/isambard/gautam.isambard.toml"
source "$G/.venv-isambard/bin/activate"
python -c "import torch; assert torch.cuda.is_available(), 'No GPU visible'; print('GPU:', torch.cuda.get_device_name(0))" || exit 1
curl -s -o /dev/null -m 30 "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=protein&format=json&pageSize=1" \
  || { echo "Europe PMC not reachable from this node"; exit 1; }

echo "=== 0. open-access REVIEW articles on broad themes (full text) ==="
gautam harvest count --reviews
gautam harvest collect --reviews
gautam harvest download --to harvest_xml
gautam harvest ingest --from harvest_xml
echo "=== 1. how many records per topic (all years, all access) ==="
gautam harvest count --scope all
echo "=== 2. fetch titles + abstracts ==="
gautam harvest abstracts
echo "=== 3. fingerprint them on the H100 ==="
gautam index --stage embed
gautam status
[ -f tests/eval_pduarticles.json ] && { echo "=== 4. search quality check ==="; gautam eval; }
ls -lh data_isambard/gautam.sqlite
