# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A personal pipeline for collecting academic economics job postings, extracting structured fields from them with a local LLM, and reviewing the results by hand. All real code lives in `econ-jobs/`. The root `main.py` is an unused PyCharm placeholder.

There are no tests, linter config, or build step. Dependencies are in `requirements.txt` (`pip install -r requirements.txt`).

## Running

All scripts use paths relative to `econ-jobs/` (`data/...`), so run them from that directory:

```bash
cd econ-jobs
python scrape.py          # download job pages (Camoufox headless browser)
python parse.py           # parse cached HTML + LLM extraction -> CSV
streamlit run app.py      # review/edit the resulting CSVs
```

`parse.py` needs a local Ollama server at `http://localhost:11434` with the model named in `MODEL` pulled (e.g. `ollama run qwen2.5:14b`).

## Pipeline architecture

The three stages share data only through files under `econ-jobs/data/`:

1. **Listing pages → job links → cached HTML (`scrape.py`).** Hand-saved search-result pages are regex-scanned for posting URLs. They are read either from `data/<site>/...-p*.html` on disk or, when a `zipfile` is passed, from `data/postings/<site>.zip`; zip entries are matched on the filename part of `adlist_pattern`. `JobExtractor` subclasses (one per site) visit each new URL with Camoufox, sleeping 20–30s between requests, and append pages to a zip (`output_zip`, default `data/html_cache/<site>.zip`) as entries `<site>/<n>.html`, the layout `parse.py` reads. Visited URLs are appended to a per-site link file (`data/cvjobs.txt`, `data/hredjobs.txt`), which is how reruns skip already-downloaded pages. Page numbering continues from the link file's line count; writing a number that already exists in the zip raises `FileExistsError`.
2. **Cached HTML → structured records (`parse.py`).** `JobParser` (ABC) defines the workflow; each site subclass implements `_get_id`, `_get_text`, `_get_link`, `_get_date_posted`, `_get_csv_path`. Input is either a directory (`extract_all_from_dir`) or a zip of the cache (`extract_all_from_zip`, e.g. `data/html_cache/cv1.zip`).
   - `extract_basics` does deterministic BeautifulSoup parsing; failures are skipped, not retried.
   - `extract_advanced` sends the markdownified text to the LLM via `instructor` (JSON mode) using the pydantic schemas `JobKeywords` and `RequiredDocuments`. Only these LLM failures are retried, for up to 10 rounds.
   - Records whose `id` already exists in the site's CSV (`_get_csv_path`), or earlier in the same run, are skipped. New rows are merged with the existing CSV when the column sets match.
3. **Review (`app.py`).** A Streamlit editor over the per-site CSVs listed in `DATASET_OPTIONS` (`hedjobs.csv`, `cvjobs.csv`, `ejmjobs.csv`, `joejobs.csv`). It supports filtering, bulk edits, and saving back to the same CSV. `parse.py` adds a `status` column for this manual triage.

Site keys used across stages: `cv` = Chronicle Vitae (jobs.chronicle.com), `hedj` = HigherEdJobs, `ejm` = EconJobMarket, `joe` = AEA JOE. All four have scrapers; only `cv` and `hedj` have parsers (`EconJobMarketParser` is a stub, and there is no JOE parser).

## Gotchas

- Zips created on macOS contain `__MACOSX/` and `._*.html` AppleDouble entries. These are binary metadata, not HTML, and `is_macos_metadata` filters them out.
- Chronicle pages come in two layouts: with tabs (`#job-description` / `#company-information`) and without (body under a hidden `<h2>Job Details</h2>`). Expired postings have no apply button, and some cached pages are "Job not found" pages.
- `main()` in `parse.py` writes to `data/output.csv`, not to the parser's `_get_csv_path()`, so the dedupe-against-existing-CSV step only takes effect once results are copied into that file.
- Read CSVs with `dtype={"id": str}`. Otherwise numeric-looking IDs load as ints and never match freshly parsed string IDs.
