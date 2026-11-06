"""Download Chronicle Vitae economics search-result pages to data/cv/chrvt-p<n>.html.

CVJobsExtractor reads this location by default, so once these pages are saved,
CVJobsExtractor().download_pages() in scrape.py visits any postings they list.
Pages already on disk are skipped, so rerunning resumes after a failure.
"""
import asyncio
import random
from pathlib import Path

from camoufox import AsyncCamoufox

from scrape import extract_cv_links

HOMEPAGE = "https://jobs.chronicle.com/"
LISTING_URL = "https://jobs.chronicle.com/jobs/economics/{}"
NUM_PAGES = 16
OUTPUT_DIR = Path("data/cv")


async def download_listings():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pending = [n for n in range(1, NUM_PAGES + 1) if not (OUTPUT_DIR / f"chrvt-p{n}.html").exists()]
    if not pending:
        print("All pages already downloaded.")
        return

    async with AsyncCamoufox(headless=True, humanize=True, os=["macos"]) as browser:
        page = await browser.new_page()
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(random.randint(3, 5))

        for n in pending:
            url = LISTING_URL.format(n)
            print(f"Visiting {url} ...")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response is not None and not response.ok:
                raise RuntimeError(f"HTTP {response.status} for {url}")

            rendered_html = await page.content()
            # A block page or a page past the last result has no postings; don't save it as a listing
            num_links = len(extract_cv_links(rendered_html))
            if num_links == 0:
                raise RuntimeError(f"No job links found on {url}")

            path = OUTPUT_DIR / f"chrvt-p{n}.html"
            path.write_text(rendered_html, encoding="utf-8")
            print(f"Saved {path}: {num_links} job links, final url {page.url}")

            if n != pending[-1]:
                nsleep = random.uniform(20, 30)
                print(f"Sleeping for {nsleep:.0f} seconds")
                await asyncio.sleep(nsleep)


if __name__ == "__main__":
    asyncio.run(download_listings())
