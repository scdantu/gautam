#!/bin/bash
# One-time setup on an Isambard-AI LOGIN node (it has internet; compute nodes may not).
# Usage:  bash <your gautam folder>/isambard/setup.sh   (works wherever the folder is)
set -euo pipefail
G="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the gautam folder
cd "$G"
echo "Gautam folder: $G"

echo "== 1. Python environment (uv) =="
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
[ -d .venv-isambard ] || uv venv .venv-isambard --python 3.12
source .venv-isambard/bin/activate

echo "== 2. PyTorch with CUDA for ARM (aarch64) =="
# PyTorch publishes CUDA builds for ARM servers on its own index. If this fails, see
# https://docs.isambard.ac.uk (PyTorch / containers on ARM) and install torch that way instead.
uv pip install torch --index-url https://download.pytorch.org/whl/cu128

echo "== 3. Gautam =="
uv pip install -e "$G"

echo "== 4. Download the embedding model into the project folder =="
export HF_HOME="$G/.hf"
python -c "from sentence_transformers import SentenceTransformer as S; S('Qwen/Qwen3-Embedding-0.6B', device='cpu'); print('model cached in', '$HF_HOME')"

echo "== 5. Checks =="
python -c "import torch; print('torch', torch.__version__, '| built with CUDA:', torch.version.cuda)"
echo "(torch.cuda.is_available() is False here on the login node; that is expected. The job checks it on the GPU.)"
cd "$G" && GAUTAM_NO_WAL=1 GAUTAM_CONFIG=isambard/gautam.isambard.toml gautam harvest count

echo
echo "Done. Next, from $G:"
echo "   source .venv-isambard/bin/activate"
echo "   (test)     see isambard/README.md, 'Quick test'"
echo "   (big run)  sbatch isambard/run.sbatch"
