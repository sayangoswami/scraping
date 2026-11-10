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
3. **Review (`app.py`).** A Streamlit editor over every CSV in a folder chosen in the sidebar (default `data/`; rescanned on each rerun; rankings.py's reference tables are excluded; `DATASET_NAMES` only gives known files a display name and order). It is schema-agnostic: it works on any CSV, not only job postings. It supports filtering, bulk edits, and saving back to the same CSV. `parse.py` adds a `status` column for this manual triage.
   - Date-range filters cover `DATE_COLUMNS` plus any text column whose non-blank values are mostly ISO dates (job date columns stay listed because they mix in free text such as "ongoing").
   - The table is read-only by default (`st.dataframe` with single-cell selection, since `st.data_editor` has no click events); clicking any cell opens a details popup for its row, whose ✏️ Edit button turns it into a form over every column (text columns with at most `MAX_ENUM_OPTIONS` repeated distinct values get a dropdown that also accepts new values; numbers are text boxes so they can be blanked). The table's key is fixed per file so a header-click sort survives saves. Whole-number columns with blanks load as `Int64`, so they save as `1`, not `1.0`. The ✏️ Grid edit toggle swaps in the editable `st.data_editor` instead (no popup there); switching modes rebuilds the table from the master dataframe. The popup's heading is `job_title`/`position`/`title`/`name`, else the first short text value. A `status` column gets a picker there: the triage codes when every value is a code or `|`-joined codes, else the column's own distinct values (at most `MAX_PICKER_OPTIONS`), else no picker.
   - On load, `rankings.py` fuzzy-matches each posting's `university` (using `location` to pin the US state) against `data/CarnegieClassification.csv` and looks up QS / Times ranks by IPEDS id (`Cuid` = `unitid`). The resulting `rank_flag` (`skip` = R1/R2, QS-ranked, or Times-ranked; `ok`; `no match`) and detail columns are read-only and dropped on save. Run `python rankings.py` to print all matches for checking.

`merge.py` combines every per-site `data/*jobs.csv` into `data/alljobs.csv` (which the app then lists first). Rows merge on a matching application link (tracking params stripped; generic links such as bare domains or a link shared by different rows of one file are ignored) or on an identical normalized title word-set plus normalized university; a title match never merges two rows of the same file. `sources` records the merged `<file>:<id>` rows. Status codes: `...` unseen, `n` no, `m` maybe, `y` yes, `1` yes with priority, `d` applied. Merged status: `d` from any source wins; else a single status chosen in the previous `alljobs.csv` (so triage and conflict resolutions there survive re-runs); else the per-site decided statuses, joined with `|` when they disagree. Other edits made in `alljobs.csv` are overwritten on re-run, so make those in the per-site CSVs. Note `ejmjobs.csv` currently holds the same Chronicle postings as `cvjobs.csv`.

Site keys used across stages: `cv` = Chronicle Vitae (jobs.chronicle.com), `hedj` = HigherEdJobs, `ejm` = EconJobMarket, `joe` = AEA JOE. All four have scrapers (`scrape.py`) and parsers (`parse.py`). EJM pages carry JSON-LD and an `ad-<id>` div; JOE pages have no structured data, so fields are regex-matched from the text under `div.listings-wrapper`.

`MiscJobParser` (→ `data/miscjobs.csv`) handles hand-saved pages from any other site, with no scraper. Its basic fields are deliberately non-LLM and may be left empty for manual entry: `date_posted` and the posting URL come only from a schema.org `JobPosting` in JSON-LD (other JSON-LD types and the canonical URL often describe a search page shared by every job), else a labelled value such as "Date posted" / "Placed On"; `link` is the first "Apply" anchor, else the posting URL; Chrome's `<!-- saved from url=... -->` comment is used when present. Text is the JobPosting `description` when present (prefixed with its title, organization, location and closing date), else `trafilatura.extract(..., favor_recall=True)`, which keeps bulleted lists the default drops.

## Gotchas

- Zips created on macOS contain `__MACOSX/` and `._*.html` AppleDouble entries. These are binary metadata, not HTML, and `is_macos_metadata` filters them out.
- Chronicle pages come in two layouts: with tabs (`#job-description` / `#company-information`) and without (body under a hidden `<h2>Job Details</h2>`). Expired postings have no apply button, and some cached pages are "Job not found" pages.
- `main()` in `parse.py` writes to `data/output.csv`, not to the parser's `_get_csv_path()`, so the dedupe-against-existing-CSV step only takes effect once results are copied into that file.
- Read CSVs with `dtype={"id": str}`. Otherwise numeric-looking IDs load as ints and never match freshly parsed string IDs.
