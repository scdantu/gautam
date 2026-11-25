# Running Project Gautam on Isambard-AI

**In one line:** copy the gautam folder to Isambard, install once, then use the GPU for the slow step
(fingerprinting). Your folder there: `/projects/u6wf/scdantu/Projects/gautam` (written `G` below).
All scripts work from wherever the folder is. Submit jobs from inside it.

## 1. Get the code, then copy the papers (code via GitHub, papers via rsync)
On Isambard (first time; afterwards just `git pull` inside the folder):
```
cd /projects/u6wf/scdantu/Projects && git clone https://github.com/scdantu/gautam.git
```
If the repo is private, clone with a GitHub personal access token or an SSH key added on Isambard.

From your Mac, copy what git deliberately leaves out (papers and your current index):
```
rsync -av ~/Projects/vsprojects/gautam/pduarticles/  isambard:/projects/u6wf/scdantu/Projects/gautam/pduarticles/
rsync -av ~/Projects/vsprojects/gautam/data/gautam.sqlite  isambard:/projects/u6wf/scdantu/Projects/gautam/data/
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

## No internet on compute nodes: download outside the job, process inside it
Login nodes are for light work only, and compute nodes have no internet. So the download step only saves files
(it mostly waits on the network); all parsing and database work happens inside the GPU job.

**Option 1: login node** (a short, light transfer, like downloading a dataset):
```
cd /projects/u6wf/scdantu/Projects/gautam && source .venv-isambard/bin/activate
export GAUTAM_NO_WAL=1 GAUTAM_CONFIG=isambard/gautam.isambard.toml
gautam index --stage meta              # Crossref details for your PDFs (~900 small lookups)
gautam harvest download --to harvest_xml
sbatch isambard/run.sbatch             # ingests harvest_xml/, fingerprints, archives
```
**Option 2: nothing on the login node; download on your Mac**
```
# Isambard (instant):
gautam harvest todo --out harvest_todo.txt
# Mac:
rsync isambard:/projects/u6wf/scdantu/Projects/gautam/harvest_todo.txt .
gautam harvest download --ids harvest_todo.txt --to harvest_xml      # ~1-3 GB; delete after uploading
rsync -av harvest_xml/ isambard:/projects/u6wf/scdantu/Projects/gautam/harvest_xml/
# Isambard:
sbatch isambard/run.sbatch
```
Re-running `download` skips what it already has. Ingesting twice is harmless.

## Citation graph: grow the library along what it cites
Each round adds the missing papers with the most weight: how many library papers cite them (fewer per
step away from your own collection) times how close their abstract is to your library. Open-access ones
are then fetched as full text. Settings: `[graph]` in `gautam.isambard.toml`; `gautam graph settings`.
```
GRAPH_DRY=1 sbatch isambard/graph.sbatch   # read references, score, list the 50 heaviest; adds nothing
sbatch isambard/graph.sbatch               # 3 rounds (GRAPH_ROUNDS=5 for more), then fetch + fingerprint
```
`gautam graph coverage` gives the share of all references that point to papers you hold; it should rise
each round and level off. Stop when a round adds less than 1%.

## 5. Bring the database home; delete the PDFs there
```
rsync -av isambard:/projects/u6wf/scdantu/Projects/gautam/data_isambard/gautam.sqlite  ~/Projects/vsprojects/gautam/data/
rm -f ~/Projects/vsprojects/gautam/data/vectors*        # old search cache; rebuilt automatically
ssh isambard 'rm -r /projects/u6wf/scdantu/Projects/gautam/pdfs'   # safe: the job ran "gautam archive"
```
Then run `gautam index` once on the Mac: it recognises your PDFs by content and re-links them.

Notes: Isambard storage is not backed up, so keep the Mac copy as master. The model and `dim` must match
the Mac's settings; these files already do.
