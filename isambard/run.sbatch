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

# Which outside services can this node reach? (Isambard compute nodes reach some sites, not others.)
ok() { curl -s -o /dev/null -m 30 -w "%{http_code}" "$1" | grep -q "^2"; }
ok "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi" && NCBI=1 || NCBI=0
ok "https://api.crossref.org/works?rows=0" && CROSSREF=1 || CROSSREF=0
ok "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=protein&format=json&pageSize=1" && EPMC=1 || EPMC=0
echo "Reachable from this node: NCBI=$NCBI Crossref=$CROSSREF EuropePMC=$EPMC"

export GAUTAM_CONFIG="$G/isambard/gautam.isambard.toml"
echo "=== 1. read your PDFs (64 at a time) ==="
gautam index --stage read --workers 64

if [ "$CROSSREF" = 1 ]; then
  echo "=== 2. Crossref details for your PDFs ==="
  gautam index --stage meta
else
  echo "(skipping Crossref: not reachable here; run 'gautam index --stage meta' on the login node later)"
fi

if [ "$EPMC" = 1 ]; then
  echo "=== 3a. list open-access papers for your topics (Europe PMC search) ==="
  gautam harvest collect
fi

if [ "$NCBI" = 1 ] || [ "$EPMC" = 1 ]; then
  SRC=both; [ "$EPMC" = 1 ] || SRC=ncbi; [ "$NCBI" = 1 ] || SRC=epmc
  echo "=== 3b. download full text ($SRC) into harvest_xml/ ==="
  gautam harvest download --to harvest_xml --source "$SRC"
fi

if [ -d harvest_xml ]; then
  echo "=== 3c. read downloaded papers (harvest_xml/) into the library ==="
  gautam harvest ingest --from harvest_xml
fi

echo "=== 4. fingerprint everything on the H100 ==="
gautam index --stage embed

echo "=== 5. archive: keep the text, stop tracking PDFs (safe to delete them now) ==="
gautam archive
gautam status
ls -lh data_isambard/gautam.sqlite
