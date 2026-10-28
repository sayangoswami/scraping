import asyncio
import fnmatch
import glob
import html
import os
import random
import re
from os.path import exists
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZipFile, ZIP_DEFLATED

from camoufox import AsyncCamoufox


def _read_html(file_name):
    return Path(file_name).read_text(encoding="utf-8", errors="ignore")

def _is_macos_metadata(file_name):
    # macOS zips include AppleDouble files (__MACOSX/..., ._name.html) that are not HTML
    return file_name.startswith('__MACOSX/') or os.path.basename(file_name).startswith('._')

def _unique_in_order(items):
    seen = set()
    unique = []

    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    return unique

def extract_posting_links(page_html, pattern, homepage):
    text = unquote(html.unescape(page_html))
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    links = []
    for match in matches:
        link = match.strip()
        if link.startswith("/"):
            link = f"{homepage}{link}"
        links.append(link)
    return _unique_in_order(links)

def extract_cv_links(page_html):
    pattern = r'(?:https://jobs\.chronicle\.com)?/job/\d+[^"\'<>\s]*'
    homepage = "https://jobs.chronicle.com"
    return extract_posting_links(page_html, pattern, homepage)

def extract_hed_links(page_html):
    pattern = r'(?:https://www\.higheredjobs\.com)?/faculty/details\.cfm\?JobCode=\d+[^"\'<>\s]*'
    homepage = "https://www.higheredjobs.com"
    return extract_posting_links(page_html, pattern, homepage)

class JobExtractor:
    def __init__(self, zipfile=None, output_zip=None):
        # Listing pages are read from this zip if given, else from adlist_pattern on disk
        self.zipfile = zipfile
        if exists(self.linkfile):
            with open(self.linkfile, 'r') as file:
                self.existing_links = [line.strip() for line in file]
        else: self.existing_links = []
        self.lid = len(self.existing_links)

        # Downloaded pages are appended to this zip as <website>/<n>.html, the layout parse.py reads
        self.output_zip = Path(output_zip or f"data/html_cache/{self.website}.zip")
        self.output_zip.parent.mkdir(parents=True, exist_ok=True)

        self.new_links = self._extract_links()

    def _get_adlist_pages(self):
        if self.zipfile is None:
            for file_name in glob.glob(self.adlist_pattern):
                yield _read_html(file_name)
            return
        # Match entries by file name only, since folder layout inside the zip differs from data/
        name_pattern = os.path.basename(self.adlist_pattern)
        with ZipFile(self.zipfile, 'r') as zf:
            for file_name in zf.namelist():
                if _is_macos_metadata(file_name):
                    continue
                if fnmatch.fnmatch(os.path.basename(file_name), name_pattern):
                    yield zf.read(file_name).decode("utf-8", errors="ignore")

    def _extract_links(self):
        links = []
        for page_html in self._get_adlist_pages():
            links.extend(self.link_extractor(page_html))
        return _unique_in_order(links)

    def _save_page(self, rendered_html):
        # Reopen per page so the archive is complete on disk even if the run dies mid-way
        entry_name = f"{self.website}/{self.lid}.html"
        with ZipFile(self.output_zip, 'a', compression=ZIP_DEFLATED) as zf:
            if entry_name in zf.namelist():
                raise FileExistsError(f"{entry_name} already exists in {self.output_zip}")
            zf.writestr(entry_name, rendered_html)
        return entry_name

    async def download_pages(self):
        processed_links = []
        try:
            async with AsyncCamoufox(
                    headless=True,  # Runs silently in background
                    humanize=True,  # Spoofs human cursor movement paths
                    os=["macos"],  # Randomly impersonates Windows or macOS fingerprint
            ) as browser:
                page = await browser.new_page()
                await page.goto(self.homepage, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.randint(3, 5))

                c = 1
                for link in self.new_links:
                    if link not in self.existing_links:
                        print(f"Visiting url {link} ...")
                        await page.goto(link, wait_until="domcontentloaded", timeout=20000)
                        rendered_html = await page.content()
                        entry_name = self._save_page(rendered_html)
                        print(f"Saved {len(rendered_html)} bytes in {self.output_zip}:{entry_name} ({c} / {len(self.new_links)} files)")
                        nsleep = random.uniform(20, 30)
                        print(f"Sleeping for {nsleep} seconds")
                        await asyncio.sleep(nsleep)
                        self.lid += 1
                        processed_links.append(link)
                    else:
                        print(f"Skipping already processed link ({c} / {len(self.new_links)}: {link}")
                    c += 1
        except TimeoutError:
            print("⚠️ Site timed out! Saving partial data and exiting gracefully...")
        except Exception as e:
            print(f"❌ An unexpected error occurred on Site: {e}")
        finally:
            print(f"Processed {len(processed_links)} links. Saving...")
            with open(self.linkfile, 'a') as file:
                for link in processed_links:
                    file.write(f"{link}\n")


class HEdJobsExtractor(JobExtractor):
    def __init__(self, zipfile=None, output_zip=None):
        self.website = "hedj"
        self.linkfile = "data/hredjobs.txt"
        self.adlist_pattern = "data/hedj/hredjobs-p*.html"
        self.homepage = "https://www.higheredjobs.com/faculty"
        self.link_extractor = extract_hed_links
        super().__init__(zipfile, output_zip)


class CVJobsExtractor(JobExtractor):
    def __init__(self, zipfile=None, output_zip=None):
        self.website = "cv"
        self.linkfile = "data/cvjobs.txt"
        self.adlist_pattern = "data/cv/chrvt-p*.html"
        self.homepage = "https://jobs.chronicle.com/"
        self.link_extractor = extract_cv_links
        super().__init__(zipfile, output_zip)


async def main():
    await CVJobsExtractor(zipfile="data/postings/cv.zip").download_pages()


if __name__ == "__main__":
    asyncio.run(main())