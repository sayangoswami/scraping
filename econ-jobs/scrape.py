import asyncio
import glob
import html
import random
import re
from os.path import exists
from pathlib import Path
from urllib.parse import unquote

from camoufox import AsyncCamoufox


def _read_html(file_name):
    return Path(file_name).read_text(encoding="utf-8", errors="ignore")

def _unique_in_order(items):
    seen = set()
    unique = []

    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    return unique

def extract_posting_links(file_name, pattern, homepage):
    text = unquote(html.unescape(_read_html(file_name)))
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    links = []
    for match in matches:
        link = match.strip()
        if link.startswith("/"):
            link = f"{homepage}{link}"
        links.append(link)
    return _unique_in_order(links)

def extract_cv_links(file_name):
    pattern = r'(?:https://jobs\.chronicle\.com)?/job/\d+[^"\'<>\s]*'
    homepage = "https://jobs.chronicle.com"
    return extract_posting_links(file_name, pattern, homepage)

def extract_hed_links(file_name):
    pattern = r'(?:https://www\.higheredjobs\.com)?/faculty/details\.cfm\?JobCode=\d+[^"\'<>\s]*'
    homepage = "https://www.higheredjobs.com"
    return extract_posting_links(file_name, pattern, homepage)

class JobExtractor:
    def __init__(self):
        if exists(self.linkfile):
            with open(self.linkfile, 'r') as file:
                self.existing_links = [line.strip() for line in file]
        else: self.existing_links = []
        self.lid = len(self.existing_links)

        self.output_dir = Path(f"data/html_cache/{self.website}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.new_links = self._extract_links()

    def _extract_links(self):
        links = []
        for file_name in glob.glob(self.adlist_pattern):
            links.extend(self.link_extractor(file_name))
        return _unique_in_order(links)

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
                        output_file = self.output_dir / f"{self.lid}.html"
                        with open(output_file, "w") as f:
                            f.write(rendered_html)
                        print(f"Saved {len(rendered_html)} bytes in {self.lid}.html ({c} / {len(self.new_links)} files)")
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
    def __init__(self):
        self.website = "hedj"
        self.linkfile = "data/hredjobs.txt"
        self.adlist_pattern = "data/hedj/hredjobs-p*.html"
        self.homepage = "https://www.higheredjobs.com/faculty"
        self.link_extractor = extract_hed_links
        super().__init__()


class CVJobsExtractor(JobExtractor):
    def __init__(self):
        self.website = "cv"
        self.linkfile = "data/cvjobs.txt"
        self.adlist_pattern = "data/cv/chrvt-p*.html"
        self.homepage = "https://jobs.chronicle.com/"
        self.link_extractor = extract_cv_links
        super().__init__()


async def main():
    await CVJobsExtractor().download_pages()


if __name__ == "__main__":
    asyncio.run(main())