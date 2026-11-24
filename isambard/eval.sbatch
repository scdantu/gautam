#!/bin/bash
#SBATCH --job-name=gautam-eval
#SBATCH --gpus=1
#SBATCH --time=01:00:00
#SBATCH --output=gautam-eval-%j.out
# Fingerprint the rebuilt index on the H100, then score old (v1) and new (v2) side by side.
set -uo pipefail
# Submit from inside your gautam folder:  cd <gautam folder> && sbatch isambard/<this file>
G="${SLURM_SUBMIT_DIR:-$PWD}"
cd "$G" || exit 1
[ -f isambard/setup.sh ] || { echo "Submit this from your gautam folder (cd there first)."; exit 1; }
export HF_HOME="$G/.hf" HF_HUB_OFFLINE=1 GAUTAM_NO_WAL=1
source "$G/.venv-isambard/bin/activate"
python -c "import torch; assert torch.cuda.is_available(), 'No GPU visible'; print('GPU:', torch.cuda.get_device_name(0))" || exit 1

echo "=== v2: read anything not yet read (normally done already on the login node), then fingerprint ==="
GAUTAM_CONFIG=isambard/eval_v2.toml gautam index --stage read --workers 32
GAUTAM_CONFIG=isambard/eval_v2.toml gautam index --stage embed

echo; echo "################ v1: index built on the Mac (old PDF reader) ################"
GAUTAM_CONFIG=isambard/eval_v1.toml gautam eval
echo; echo "################ v2: rebuilt with the new PDF reader ################"
GAUTAM_CONFIG=isambard/eval_v2.toml gautam eval
GAUTAM_CONFIG=isambard/eval_v2.toml gautam status
