#!/bin/bash
#SBATCH --job-name=gautam
#SBATCH --gpus=1
#SBATCH --time=24:00:00
#SBATCH --output=gautam-%j.out
# One GH200 = 72 Grace CPU cores (reading PDFs) + one H100 (fingerprints).
# Every stage saves as it goes: if the job hits the time limit, just submit it again.
set -uo pipefail

# Submit from inside your gautam folder:  cd <gautam folder> && sbatch isambard/<this file>
G="${SLURM_SUBMIT_DIR:-$PWD}"
cd "$G" || exit 1
[ -f isambard/setup.sh ] || { echo "Submit this from your gautam folder (cd there first)."; exit 1; }
export HF_HOME="$G/.hf" HF_HUB_OFFLINE=1 GAUTAM_NO_WAL=1
source "$G/.venv-isambard/bin/activate"

python -c "import torch; assert torch.cuda.is_available(), 'No GPU visible'; print('GPU:', torch.cuda.get_device_name(0))" || exit 1

NET=0
curl -s -m 15 -o /dev/null "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=protein&format=json&pageSize=1" && NET=1
echo "Internet from compute node: $NET"

echo "=== 1. read your PDFs (64 at a time) ==="
export GAUTAM_CONFIG="$G/isambard/gautam.isambard.toml"
gautam index --stage read --workers 64

if [ "$NET" = 1 ]; then
  echo "=== 2. Crossref details for your PDFs ==="
  gautam index --stage meta
  echo "=== 3. harvest open-access papers for your topics ==="
  gautam harvest collect
  gautam harvest fetch --workers 8
else
  echo "!!! No internet here. Run these on the LOGIN node instead, then resubmit this job:"
  echo "    gautam index --stage meta && gautam harvest collect && gautam harvest fetch --workers 8"
fi

echo "=== 4. fingerprint everything on the H100 ==="
gautam index --stage embed

echo "=== 5. archive: keep the text, stop tracking PDFs (safe to delete them now) ==="
gautam archive
gautam status
ls -lh data/gautam.sqlite
