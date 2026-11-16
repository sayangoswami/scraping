"""Download job-site search-result (listing) pages with Camoufox, for scrape.py to read.

Each site is a ListingDownloader subclass. Pages are saved where that site's extractor in
scrape.py looks for them:
  - as files (default): data/<site>/<name>, read by e.g. EJMJobsExtractor()
  - with --zip: entries <site>/<name> in data/postings/<site>.zip,
    read by e.g. EJMJobsExtractor(zipfile="data/postings/ejm.zip")

Pages already saved are skipped, so rerunning resumes after a failure. Listings change over
time, so pass --refresh to replace a site's saved listing pages with fresh ones.

Usage (from econ-jobs/):
    python scrape_listings.py ejm
    python scrape_listings.py ejm cv --zip --refresh
    python scrape_listings.py ejm --pages 8
    python scrape_listings.py joe --issue 2026-02

To add a site: subclass ListingDownloader with its key, URLs, page count, file-name pattern
(matching the extractor's adlist_pattern in scrape.py) and link extractor, then add it to SITES.
"""
import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import random
import tempfile
import zlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from camoufox import AsyncCamoufox

from scrape import extract_cv_links, extract_ejm_links, extract_joe_links


class ListingDownloader:
    site = None           # site key used by scrape.py and parse.py, e.g. "ejm"
    homepage = None       # visited first so the session looks like a normal visitor's
    listing_url = None    # search-result URL with {n} for the page number
    num_pages = None      # pages 1..num_pages are downloaded (override with --pages)
    file_name = None      # saved page name with {n}; must match the extractor's adlist_pattern
    link_extractor = None # scrape.py function returning the posting links on a listing page
    timeout_ms = 20000    # page load timeout

    def __init__(self, use_zip=False, num_pages=None):
        self.use_zip = use_zip
        self.num_pages = num_pages or self.num_pages
        self.folder = Path("data") / self.site
        self.zip_path = Path("data/postings") / f"{self.site}.zip"

    def page_url(self, n):
        return self.listing_url.format(n)

    def page_name(self, n):
        return self.file_name.format(n)

    def _saved_names(self):
        if self.use_zip:
            if not self.zip_path.exists():
                return set()
            with ZipFile(self.zip_path) as zf:
                return {os.path.basename(name) for name in zf.namelist()}
        return {p.name for p in self.folder.glob("*")} if self.folder.exists() else set()

    def clear(self):
        """Remove this site's saved listing pages (only names this downloader writes)."""
        names = {self.page_name(n) for n in range(1, self.num_pages + 1)}
        if self.use_zip:
            if not self.zip_path.exists():
                return
            # Zips can't delete entries, so copy everything else to a new archive
            with ZipFile(self.zip_path) as src, \
                    tempfile.NamedTemporaryFile(dir=self.zip_path.parent, suffix=".zip", delete=False) as tmp:
                with ZipFile(tmp, "w", compression=ZIP_DEFLATED) as dst:
                    for item in src.infolist():
                        if os.path.basename(item.filename) not in names:
                            dst.writestr(item, src.read(item))
            os.replace(tmp.name, self.zip_path)
        else:
            for name in names:
                (self.folder / name).unlink(missing_ok=True)

    def save(self, n, page_html):
        name = self.page_name(n)
        if self.use_zip:
            self.zip_path.parent.mkdir(parents=True, exist_ok=True)
            # Reopen per page so the archive is complete on disk even if the run dies mid-way
            with ZipFile(self.zip_path, "a", compression=ZIP_DEFLATED) as zf:
                zf.writestr(f"{self.site}/{name}", page_html)
            return f"{self.zip_path}:{self.site}/{name}"
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / name
        path.write_text(page_html, encoding="utf-8")
        return str(path)

    async def download(self):
        saved = self._saved_names()
        pending = [n for n in range(1, self.num_pages + 1) if self.page_name(n) not in saved]
        if not pending:
            print(f"[{self.site}] All {self.num_pages} pages already saved (use --refresh to re-download).")
            return

        async with AsyncCamoufox(headless=True, humanize=True, os=["macos"]) as browser:
            page = await browser.new_page()
            await page.goto(self.homepage, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await asyncio.sleep(random.randint(3, 5))

            for n in pending:
                url = self.page_url(n)
                print(f"[{self.site}] Visiting {url} ...")
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                if response is not None and not response.ok:
                    raise RuntimeError(f"HTTP {response.status} for {url}")

                page_html = await page.content()
                # A block page or a page past the last result has no postings; don't save it
                num_links = len(type(self).link_extractor(page_html))
                if num_links == 0:
                    raise RuntimeError(f"No job links found on {url} (final url {page.url})")

                where = self.save(n, page_html)
                print(f"[{self.site}] Saved {where}: {num_links} job links")

                if n != pending[-1]:
                    nsleep = random.uniform(20, 30)
                    print(f"Sleeping for {nsleep:.0f} seconds")
                    await asyncio.sleep(nsleep)


class EJMListingDownloader(ListingDownloader):
    site = "ejm"
    homepage = "https://econjobmarket.org/"
    listing_url = "https://econjobmarket.org/positions?page={}"
    num_pages = 7
    file_name = "p{}.html"            # EJMJobsExtractor reads data/ejm/p*.html
    link_extractor = extract_ejm_links


class CVListingDownloader(ListingDownloader):
    site = "cv"
    homepage = "https://jobs.chronicle.com/"
    listing_url = "https://jobs.chronicle.com/jobs/economics/{}"
    num_pages = 16
    file_name = "chrvt-p{}.html"      # CVJobsExtractor reads data/cv/chrvt-p*.html
    link_extractor = extract_cv_links


def joe_issue(today=None):
    """JOE issue covering a date: YYYY-01 runs February-July, YYYY-02 runs August-January."""
    today = today or dt.date.today()
    if today.month >= 8:
        return f"{today.year}-02"
    if today.month == 1:
        return f"{today.year - 1}-02"
    return f"{today.year}-01"


class JOEListingDownloader(ListingDownloader):
    """All of the current issue's postings on one page (lpp=all)."""
    site = "joe"
    homepage = "https://www.aeaweb.org/joe/listings"
    num_pages = 1
    file_name = "p{}.html"            # JOEJobsExtractor reads data/joe/p*.html
    link_extractor = extract_joe_links
    timeout_ms = 90000                # the all-postings page is several MB
    issue = None                      # set by --issue; defaults to the current issue

    def page_url(self, n):
        # The search form is passed as q=: zlib-compressed JSON in URL-safe base64, with "," for "="
        issue = self.issue or joe_issue()
        form = {"ListingsForm": {
            "in_active": "0", "keyword_search": "", "section": None, "jel": None,
            "issue": issue, "originalIssue": issue, "country_region": None, "saved_folders": None,
            "candidate_scramble": None, "recent_only": None, "listings_order_by": "date_posted_a",
            "lpp": "all", "page": None,
        }}
        raw = json.dumps(form, separators=(",", ":")).encode()
        q = base64.urlsafe_b64encode(zlib.compress(raw)).decode().replace("=", ",")
        return f"https://www.aeaweb.org/joe/listings?q={q}"


SITES = {cls.site: cls for cls in [EJMListingDownloader, CVListingDownloader, JOEListingDownloader]}


async def main():
    parser = argparse.ArgumentParser(description="Download job-site listing pages for scrape.py.")
    parser.add_argument("sites", nargs="+", choices=list(SITES), help="site keys to download")
    parser.add_argument("--zip", action="store_true", help="save into data/postings/<site>.zip instead of data/<site>/")
    parser.add_argument("--refresh", action="store_true", help="replace previously saved listing pages")
    parser.add_argument("--pages", type=int, help="number of pages to download (default: the site's own)")
    parser.add_argument("--issue", help="JOE issue, e.g. 2026-02 (default: the current one)")
    args = parser.parse_args()

    for site in args.sites:
        downloader = SITES[site](use_zip=args.zip, num_pages=args.pages)
        if site == "joe" and args.issue:
            downloader.issue = args.issue
        if args.refresh:
            downloader.clear()
        await downloader.download()


if __name__ == "__main__":
    asyncio.run(main())
