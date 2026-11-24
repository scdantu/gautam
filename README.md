# Project Gautam

*Named after the rishi Gautama, who wanted to read everything.*

Gautam turns your folder of PDFs into a library Claude can search. Claude answers from the actual paragraphs
in your papers, and cites the paper and page each point came from.

**A picture:** a librarian with a card catalogue. Each paragraph of every paper gets a card. The card notes
what the paragraph means, which paper it's from and which page it's on. You ask a question, the librarian
brings the best five cards, and Claude writes the answer from them.

## What it does

1. **Search** your papers by meaning and by exact keywords (e.g. "TEM-1", "loop 6") together.
2. **Keeps itself up to date:** it reads only new, changed, moved or deleted PDFs, never the whole library again.
3. **Checks DOIs** in any `.bib` against Crossref (the official DOI registry). It catches fake DOIs, broken
   DOIs, missing DOIs, mismatched titles, years and authors, and retracted papers. It writes a report and a
   corrected copy (`*.checked.bib`). Your original file is never changed.
4. **Fetches missing papers** for a `.bib`. It downloads free legal copies (arXiv, Unpaywall, Europe PMC)
   into `inbox/`. Everything else goes on a checklist of library links for you to click while signed in.
5. **Exports BibTeX** for any set of papers, ready for manuscripts and grants.
6. **One-page summaries:** Claude reads the full text and fills a fixed template, saved in `data/summaries/`.

Everything runs on your Mac. The only things that go online are the model download (once) and DOI/metadata
lookups.

## Setup (about 10 minutes, once)

1. **Install `uv`** (it manages Python for you). In Terminal:
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Close and reopen Terminal, then check with `which uv`.
2. **Make your settings file:**
   ```
   cd ~/Projects/vsprojects/gautam
   uv run gautam init
   ```
   Open `gautam.toml` and fill in:
   - `dirs`: your OneDrive papers folder
   - `email`: Crossref and Unpaywall ask for one
   - `prefix` (optional): the Brunel library proxy prefix
3. **Trial run on 20 papers:**
   ```
   uv run gautam index --limit 20
   uv run gautam search "omega loop dynamics in class A beta-lactamases"
   ```
   The first run downloads the Qwen3 embedding model (about 1.2 GB) and installs PyTorch, so it's slow.
   After that it's fast.
   Indexing runs in three stages. Each stage finishes for every paper before the next one starts, and
   progress is saved as it goes, so an interrupted run picks up where it stopped:
   1. **read** all PDFs, several at once (`--workers 4`);
   2. **meta**: look up details on Crossref, several lookups at once, still polite to Crossref;
   3. **embed**: fingerprint every passage in big GPU batches.

   You can run one stage on its own, e.g. `gautam index --stage read`. Keyword search works as soon as
   stage 1 is done.
4. **Index the rest in batches**, so OneDrive never has to download everything at once:
   ```
   uv run gautam index --limit 50
   ```
   Repeat until it says nothing is waiting. After each batch you can right-click the OneDrive folder and
   choose **Free Up Space**. The index keeps the text, so the PDFs can go back to the cloud.
5. **Connect it to Claude Desktop.** Go to Settings → Developer → Edit Config and add this (use the path
   that `which uv` printed):
   ```json
   {
     "mcpServers": {
       "gautam": {
         "command": "/Users/sarathdantu/.local/bin/uv",
         "args": ["--directory", "/Users/sarathdantu/Projects/vsprojects/gautam", "run", "gautam-mcp"]
       }
     }
   }
   ```
   Restart Claude Desktop. Then try: *"Using Gautam, what do my papers say about Ω-loop flexibility?"*

## Big runs on Isambard-AI

Gautam can also index your PDFs and harvest open-access full text for your topics (Europe PMC) on
Isambard-AI, then hand back one database file. See [`isambard/README.md`](isambard/README.md). The commands:
- `gautam harvest count`: dry run; how many papers each topic finds
- `gautam harvest collect`: list them, without duplicates
- `gautam harvest fetch`: download their text
- `gautam archive`: keep the text, stop tracking PDFs (so they can be deleted)

## Everyday use (just ask Claude)

| You say | Gautam tool |
|---|---|
| "What do my papers say about X?" | `search_papers` (adds up to 5 new PDFs first) |
| "Update my library" | `update_library` |
| "One-page summary of dantu2019omega" | `get_paper_text` → `save_summary` |
| "Check the DOIs in ~/grant/refs.bib" | `verify_bib` |
| "Get the papers in this .bib that I don't have" | `fetch_missing` |
| "Make a .bib of these 12 papers for the manuscript" | `export_bib` |
| "Re-check everything for retractions" | `refresh_metadata(only_missing=false)` |

The same jobs are available from the command line: `uv run gautam --help`.

## Where things live

- `data/gautam.sqlite`: the whole index (roughly 100–200 MB per 1,000 papers). Keep it **outside** OneDrive.
- `data/summaries/`, `data/bib/`, `data/reports/`: your summaries, exported `.bib` files and DOI-check reports.
- `inbox/`: drop PDFs here (fetched or downloaded by hand); they're indexed like everything else.

## Known limits, and what to do about each

1. **Scanned PDFs (images, no text)** are reported as failed. *Fix:* run OCR on them, e.g. in Preview or
   with `ocrmypdf`, and drop them back in.
2. **Papers with no DOI on their first page** get their details from the PDF title, which is less reliable.
   *Fix:* run `uv run gautam refresh`, which looks them up in Crossref by title, or check the citekey before
   citing. `export_bib` lists any entries it couldn't verify.
3. **Changing the model or `dim`** means rebuilding the index. Gautam refuses to mix two kinds of fingerprints.
4. **Paywalled papers** can't be downloaded automatically. Gautam never uses your password. It gives you a
   checklist of links to click while you're signed in to the Brunel library.

## Tests

```
uv run --with pytest pytest -q
```
These run offline with fake PDFs, a stand-in embedder and a fake Crossref.
