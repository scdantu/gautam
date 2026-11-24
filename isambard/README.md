# Running Project Gautam on Isambard-AI

**In one line:** copy the gautam folder to Isambard, install once, then use the GPU for the slow step
(fingerprinting). Your folder there: `/projects/u6wf/scdantu/Projects/gautam` (written `G` below).
All scripts work from wherever the folder is. Submit jobs from inside it.

## 1. Copy (from your Mac)
```
rsync -av --exclude '.venv' --exclude 'data_v2' --exclude '__pycache__' \
  ~/Projects/vsprojects/gautam/  isambard:/projects/u6wf/scdantu/Projects/gautam/
```
(Use the SSH host name `clifton ssh-config write` gave you, if it isn't `isambard`.)

## 2. Install (Isambard login node, once)
```
bash /projects/u6wf/scdantu/Projects/gautam/isambard/setup.sh
```
This makes `G/.venv-isambard` (Python + PyTorch for ARM + CUDA + Gautam), puts the model in `G/.hf`,
and ends with a dry-run count of open-access papers per topic.

## 3. Quick test: old index vs rebuilt index (about 15 minutes)
```
cd /projects/u6wf/scdantu/Projects/gautam
source .venv-isambard/bin/activate
export GAUTAM_NO_WAL=1 GAUTAM_CONFIG=isambard/eval_v2.toml
gautam index --stage read          # 70 PDFs from pduarticles, new reader (seconds)
gautam index --stage meta          # Crossref details (login node has internet)
sbatch isambard/eval.sbatch        # fingerprint on the H100, then score v1 and v2
tail -f gautam-eval-*.out
```

## 4. Big run (your ~700 PDFs + open-access papers for your topics)
```
rsync -av ~/path/to/articles/  isambard:/projects/u6wf/scdantu/Projects/gautam/pdfs/   # from the Mac
cd /projects/u6wf/scdantu/Projects/gautam && sbatch isambard/run.sbatch                  # on Isambard
```
Settings: `isambard/gautam.isambard.toml` (add your email). The result goes to `G/data_isambard/gautam.sqlite`.
If the job hits 24 hours, submit it again; it carries on. If it says the compute node has no internet,
run the printed commands on the login node, then resubmit.

## 5. Bring the database home; delete the PDFs there
```
rsync -av isambard:/projects/u6wf/scdantu/Projects/gautam/data_isambard/gautam.sqlite  ~/Projects/vsprojects/gautam/data/
rm -f ~/Projects/vsprojects/gautam/data/vectors*        # old search cache; rebuilt automatically
ssh isambard 'rm -r /projects/u6wf/scdantu/Projects/gautam/pdfs'   # safe: the job ran "gautam archive"
```
Then run `gautam index` once on the Mac: it recognises your PDFs by content and re-links them.

Notes: Isambard storage is not backed up, so keep the Mac copy as master. The model and `dim` must match
the Mac's settings; these files already do.
