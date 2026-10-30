import hashlib
import json
import os
from urllib.parse import urljoin, urlparse

import pandas as pd
import re
from bs4 import BeautifulSoup, Comment
import markdownify as md
import trafilatura

from typing import List, Dict
from pydantic import BaseModel, Field
import instructor
from openai import OpenAI
from tqdm import tqdm
from zipfile import ZipFile
from abc import ABC, abstractmethod

# run `ollama run qwen2.5:14b`
# Alternatively, if you want an absolute speed demon that uses less VRAM, run ollama run llama3.1:8b or ollama run phi4:14b
MODEL = "qwen2.5:14b"   # could also be "phi4-mini:latest" or "qwen2.5:7b", or even "qwen2.5:14b" for the desktop

# 1. Define schema for categorized keywords
class JobKeywords(BaseModel):
    job_title: str = Field(description="Title of the job e.g., Assistant Professor of Economics")
    university: str = Field(description="Name of the university or institution offering the job, e.g., Salisbury University, or University of California, Berkeley")
    location: str = Field(description="Location of the job, e.g., Salisbury, MD, or Berkeley, CA")
    review_start: str = Field(description="Date from which they will begin reviewing applications (may be 'none'), e.g., September 11, 2026, or 2023-09-30")
    deadline: str = Field(description="Deadline for applying to the job (may be 'none'), e.g., September 11, 2026, or 2023-09-30")
    responsibilities: List[str] = Field(
        description="Key responsibilities or duties of the job (e.g., Undergraduate Teaching, Research, Publications, Service)"
    )
    preferred_qualifications: List[str] = Field(
        description="Preferred qualifications or experiences (e.g., Experience with Python, Strong communication skills)"
    )
    required_skills: List[str] = Field(
        description="Required technical skills, academic disciplines, or methodologies (e.g., Stata, Econometrics, Data Science)"
    )
    visa_sponsorship: str = Field(description="Visa sponsorship status, may or may not be mentioned. (e.g., Is eligible for H1B visa sponsorship, Not eligible, Not Mentioned)")


class RequiredDocuments(BaseModel):
    documents: List[str] = Field(description="List of required documents (e.g., Resume, Cover Letter, Transcripts, Teaching Statement, Research Statement, Diversity Statement, Teaching Portfolio, Letters of Recommendation)")

# 4. Extract keywords and requirements
def extract_from_post(client, job_description):
    extracted = client.chat.completions.create(
        model=MODEL,
        response_model=JobKeywords,
        messages=[
            {
                "role": "system",
                "content": "Extract key information accurately from the provided job description text."
            },
            {
                "role": "user",
                "content": job_description
            }
        ]
    )
    return extracted.model_dump()

def extract_requirements_from_post(client, job_description):
    extracted = client.chat.completions.create(
        model=MODEL,
        response_model=RequiredDocuments,
        messages=[
            {
                "role": "system",
                "content": "Extract required documents accurately from the provided job description text."
            },
            {
                "role": "user",
                "content": job_description
            }
        ]
    )
    return extracted.model_dump()

def get_markdown_from_attrs(soup, attribs):
    res = ""
    for attrib_id in attribs:
        attrib_div = soup.find(id=attrib_id)
        if attrib_div:
            attrib = md.markdownify(str(attrib_div))
            res += ('\n' + attrib)
    return res

def find_key_value_in_html(soup, key_pattern, tag_type):
    tag = soup.find(tag_type, string=re.compile(key_pattern))
    if tag:
        html_content = str(tag.string)
        match = re.search(key_pattern, html_content)
        if match:
            value = match.group(1)
            return value
    return None

def get_job_posting_ld(soup):
    """The schema.org JobPosting in the page's JSON-LD, or None.
    Other JSON-LD types (WebPage, etc.) describe the page itself, so their dates and URLs are ignored."""
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or "", strict=False)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get('@graph', [data])
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get('@type')
            if types == 'JobPosting' or (isinstance(types, list) and 'JobPosting' in types):
                return item
    return None


def find_labelled_value(soup, label_pattern):
    """Text following a label element, e.g. <th>Placed On:</th><td>29th September 2026</td>
    or <span>Date posted</span><span>2026-09-17</span>."""
    for label in soup.find_all(string=re.compile(rf'^\s*(?:{label_pattern})\s*:?\s*$', re.IGNORECASE)):
        for sibling in label.parent.next_siblings:
            text = sibling.get_text(" ", strip=True) if hasattr(sibling, 'get_text') else sibling.strip()
            if text:
                return text
    return None


def to_iso_date(value):
    if not value:
        return None
    date = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(date) else date.strftime("%Y-%m-%d")


def fix_date_format(df):
    # 1. Convert to datetime, turning malformed rows into NaT (Not a Time)
    review_start = pd.to_datetime(df["review_start"], errors="coerce", format="mixed", utc=True)
    deadline = pd.to_datetime(df["deadline"], errors="coerce", format="mixed", utc=True)

    # 2. Format the valid dates to a fixed format (e.g., 'YYYY-MM-DD')
    formatted_review_start = review_start.dt.strftime("%Y-%m-%d")
    formatted_deadline = deadline.dt.strftime("%Y-%m-%d")

    # 3. Fill the NaT gaps with the original text column
    formatted_review_start = formatted_review_start.fillna(df["review_start"])
    formatted_deadline = formatted_deadline.fillna(df["deadline"])

    df["review_start"] = formatted_review_start
    df["deadline"] = formatted_deadline
    return df


def schemas_are_equal(df1, df2):
    # Compare column names only: dtypes differ between a fresh frame and one read back from CSV
    return set(df1.columns) == set(df2.columns)


def is_macos_metadata(file_name):
    # macOS zips include AppleDouble files (__MACOSX/..., ._name.html) that are not HTML
    return file_name.startswith('__MACOSX/') or os.path.basename(file_name).startswith('._')

class JobParser(ABC):
    def __init__(self, zipfile=None, html_dir=None):
        self.client = instructor.from_openai(
            OpenAI(base_url="http://localhost:11434/v1", api_key="ollama"),
            mode=instructor.Mode.JSON
        )
        self.html_dir = html_dir
        self.zipfile = zipfile
        self.df = self.get_existing_data()

    @abstractmethod
    def _get_id(self, soup: BeautifulSoup):
        """Extract the job ID from the provided BeautifulSoup object corresponding to a job post."""
        pass

    @abstractmethod
    def _get_text(self, soup: BeautifulSoup):
        """Extract the job text from the provided BeautifulSoup object corresponding to a job post."""
        pass

    @abstractmethod
    def _get_link(self, soup: BeautifulSoup):
        """Extract the job application link from the provided BeautifulSoup object corresponding to a job post."""
        pass

    @abstractmethod
    def _get_date_posted(self, soup: BeautifulSoup):
        """Extract the date the job was posted from the provided BeautifulSoup object corresponding to a job post."""
        pass

    @abstractmethod
    def _get_csv_path(self):
        """Return the path to the CSV file where job data is/will be stored."""
        pass

    def get_existing_data(self):
        if os.path.exists(self._get_csv_path()):
            return pd.read_csv(self._get_csv_path(), dtype={"id": str})
        return None

    def get_files_from_zip(self):
        with ZipFile(self.zipfile, 'r') as zf:
            for file_name in zf.namelist():
                if file_name.endswith('.html') and not is_macos_metadata(file_name):
                    with zf.open(file_name, 'r') as f:
                        yield file_name, f.read()

    def get_files_from_dir(self):
        for file_name in os.listdir(self.html_dir):
            if file_name.endswith('.html') and not is_macos_metadata(file_name):
                with open(os.path.join(self.html_dir, file_name), 'rb') as f:
                    yield file_name, f.read()

    def extract_basics(self, soup: BeautifulSoup):
        try:
            record = {
                "id": self._get_id(soup),
                "text": self._get_text(soup),
                "link": self._get_link(soup),
                "date_posted": self._get_date_posted(soup),
            }
        except Exception as e:
            tqdm.write(f"Error extracting basic info: {e!r}")
            return None
        if not record["id"] or not record["text"]:
            return None
        return record

    def extract_advanced(self, record: Dict):
        try:
            job_info = extract_from_post(self.client, record["text"])
            job_requirements = extract_requirements_from_post(self.client, record["text"])
        except Exception as e:
            tqdm.write(f"Error extracting advanced info: {e!r}")
            return False
        record.update(job_info)
        record.update(job_requirements)
        return True

    def extract_all(self, enumerate_file):
        processed = set()   # files done, or permanently unparseable; not retried
        records = []
        n_failed = 0
        files = list(enumerate_file())
        for j in range(10):
            pending = [(name, content) for name, content in files if name not in processed]
            n_failed = 0
            for filename, html_content in tqdm(pending, desc=f"Round {j + 1}", unit="file"):
                if not self.process_file(filename, html_content, processed, records):
                    n_failed += 1
            if n_failed == 0:
                break
            else:
                print(f"{n_failed} items failed. trying again")
        if n_failed > 0:
            print(f"Reached max rounds, {n_failed} remaining still. Quitting.")
        return self.save_dataframe(records)

    def extract_all_from_dir(self):
        return self.extract_all(self.get_files_from_dir)

    def extract_all_from_zip(self):
        return self.extract_all(self.get_files_from_zip)

    def process_file(self, filename: str, html_content, processed, records):
        """Returns False only for failures worth retrying (the LLM step)."""
        if filename in processed:
            return True
        soup = BeautifulSoup(html_content, "html.parser")
        record = self.extract_basics(soup)
        if record is None:
            # Parsing is deterministic, so retrying would fail the same way
            tqdm.write(f"Failed to extract basic info from {filename}, skipping.")
            processed.add(filename)
            return True
        if self.df is not None and record['id'] in self.df['id'].values:
            tqdm.write(f"Skipping {filename} as it already exists in the database.")
            processed.add(filename)
            return True
        if any(r['id'] == record['id'] for r in records):
            tqdm.write(f"Skipping {filename} as a duplicate of an earlier file in this run.")
            processed.add(filename)
            return True
        if self.extract_advanced(record):
            records.append(record)
            processed.add(filename)
            return True
        tqdm.write(f"Failed to extract advanced info from {filename}")
        return False

    def save_dataframe(self, records):
        if not records:
            print("No new records extracted.")
            return self.df if self.df is not None else pd.DataFrame()
        df = pd.DataFrame(records)
        df = fix_date_format(df)
        df['status'] = '...'
        if self.df is not None:
            if schemas_are_equal(df, self.df):
                df = pd.concat([self.df, df], axis=0, ignore_index=True)
                print("Merged dataframes.")
            else:
                print("Schema mismatch. Cannot merge.")
        else:
            print("No existing data found. This is likely the first run.")
        return df

class HigherEdJobParser(JobParser):
    def _get_id(self, soup: BeautifulSoup):
        spans = soup.find_all('span', {'data-jc': True}, recursive=True)
        if spans:
            return spans[0]['data-jc']
        return None

    def _get_text(self, soup: BeautifulSoup):
        attribs = ['job-header', 'jobLocation', 'jobAttrib', 'jobDesc']
        return get_markdown_from_attrs(soup, attribs)

    def _get_link(self, soup: BeautifulSoup):
        link_div = soup.find(id="js-applyurl", recursive=True)
        if link_div:
            # Get 'href' or fall back to custom attributes like 'data-orig-href'
            href = link_div.get("data-orig-href")
            if href:
                return href
        return None

    def _get_date_posted(self, soup: BeautifulSoup):
        # Pattern handles arbitrary whitespace/tabs and captures the date string
        pattern = r'"datePosted"\s*:\s*"([^"]+)"'
        return find_key_value_in_html(soup, pattern, 'script')

    def _get_csv_path(self):
        return "data/hedjobs.csv"


class ChronicleVitaeParser(JobParser):

    def _get_zip_file(self):
        return self.zipfile

    def _get_html_dir(self):
        return self.html_dir

    def _get_id(self, soup: BeautifulSoup):
        return soup.find('title').string.split('|')[-1].strip()

    def _get_text(self, soup: BeautifulSoup):
        attribs = ['job-description', 'company-information']
        text = get_markdown_from_attrs(soup, attribs)
        if text:
            return text
        # Postings without a company tab have no tab ids; the body sits under a hidden "Job Details" heading
        heading = soup.find('h2', string=re.compile(r'^\s*Job Details\s*$'))
        if heading:
            return md.markdownify(str(heading.parent))
        return ""

    def _get_link(self, soup: BeautifulSoup):
        apply_link = soup.find('a', attrs={'data-hook': 'apply-button'})
        # Expired postings have no apply button
        if apply_link is None or not apply_link.get('href'):
            return None
        return 'https://jobs.chronicle.com' + apply_link['href']

    def _get_date_posted(self, soup: BeautifulSoup):
        # Pattern handles arbitrary whitespace/tabs and captures the date string
        pattern = r'"datePosted"\s*:\s*"([^"]+)"'
        return find_key_value_in_html(soup, pattern, 'script')

    def _get_csv_path(self):
        return "data/cvjobs.csv"


class EconJobMarketParser(JobParser):
    def _get_ad_div(self, soup: BeautifulSoup):
        # Closed or removed postings render a "no longer available" page without this div
        return soup.find('div', id=re.compile(r'^ad-\d+$'))

    def _get_id(self, soup: BeautifulSoup):
        ad_div = self._get_ad_div(soup)
        if ad_div:
            return ad_div['id'].removeprefix('ad-')
        return None

    def _get_text(self, soup: BeautifulSoup):
        ad_div = self._get_ad_div(soup)
        if ad_div is None:
            return ""
        # The enclosing card also holds the title, location, and deadline fields
        card = ad_div.find_parent('div', class_='card') or ad_div
        return md.markdownify(str(card))

    def _get_link(self, soup: BeautifulSoup):
        # Applications go through EJM itself, so the posting URL is the apply link
        canonical = soup.find('link', rel='canonical')
        if canonical:
            return canonical.get('href')
        return None

    def _get_date_posted(self, soup: BeautifulSoup):
        pattern = r'"datePosted"\s*:\s*"([^"]+)"'
        return find_key_value_in_html(soup, pattern, 'script')

    def _get_csv_path(self):
        return "data/ejmjobs.csv"


class JOEJobParser(JobParser):
    def _get_id(self, soup: BeautifulSoup):
        return find_key_value_in_html(soup, r'JOE ID Number:\s*([\d_-]+)', 'div')

    def _get_text(self, soup: BeautifulSoup):
        wrapper = soup.find('div', class_='listings-wrapper')
        if wrapper is None:
            return ""
        # Copy before dropping the inline application scripts so the caller's soup is untouched
        wrapper = BeautifulSoup(str(wrapper), "html.parser")
        for tag in wrapper.find_all(['script', 'style']):
            tag.decompose()
        for tag in wrapper.find_all('a', href=re.compile(r'^javascript:')):
            tag.unwrap()
        return md.markdownify(str(wrapper))

    def _get_link(self, soup: BeautifulSoup):
        apply_link = soup.find('a', string=re.compile(r'Apply for This Job'))
        # Listings that take applications through JOE use a javascript: link; fall back to the listing URL
        if apply_link and apply_link.get('href', '').startswith('http'):
            return apply_link['href']
        joe_id = self._get_id(soup)
        if joe_id:
            return f"https://www.aeaweb.org/joe/listing.php?JOE_ID={joe_id}"
        return None

    def _get_date_posted(self, soup: BeautifulSoup):
        # Shown as MM/DD/YYYY; convert to ISO like the other sites
        date_posted = find_key_value_in_html(soup, r'Date Posted:\s*(\d{2}/\d{2}/\d{4})', 'div')
        if date_posted:
            return pd.to_datetime(date_posted, format="%m/%d/%Y").strftime("%Y-%m-%d")
        return None

    def _get_csv_path(self):
        return "data/joejobs.csv"


class MiscJobParser(JobParser):
    """Pages from any other site, without per-site rules. Basic fields come only from JSON-LD,
    meta tags, and labelled text, so missing ones are left empty for filling in by hand."""

    def _get_page_url(self, soup: BeautifulSoup):
        # Chrome's "Save Page As" records the source URL in a comment
        saved_from = soup.find(string=lambda s: isinstance(s, Comment) and 'saved from url=' in s)
        if saved_from:
            match = re.search(r'saved from url=\(\d+\)(\S+)', saved_from)
            if match:
                return match.group(1)
        job = get_job_posting_ld(soup)
        if job is None:
            # Without a JobPosting the canonical URL is often a search or listing page shared by every job
            return None
        if job.get('url'):
            return job['url']
        canonical = soup.find('link', rel='canonical') or soup.find('meta', property='og:url')
        if canonical:
            return canonical.get('href') or canonical.get('content')
        return None

    def _get_id(self, soup: BeautifulSoup):
        page_url = self._get_page_url(soup)
        key = page_url or self._get_text(soup)
        if not key:
            return None
        site = urlparse(page_url).netloc.removeprefix('www.') if page_url else 'misc'
        return f"{site}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"

    def _get_text(self, soup: BeautifulSoup):
        job = get_job_posting_ld(soup) or {}
        # Fields that often sit in a sidebar the text extraction drops
        header = []
        if job.get('title'):
            header.append(f"# {job['title']}")
        organization = job.get('hiringOrganization')
        if isinstance(organization, dict) and organization.get('name'):
            header.append(f"Organization: {organization['name']}")
        locations = job.get('jobLocation') or []
        for location in locations if isinstance(locations, list) else [locations]:
            address = location.get('address') if isinstance(location, dict) else None
            if isinstance(address, dict):
                parts = [address.get(k) for k in ('addressLocality', 'addressRegion', 'addressCountry')]
                parts = [p.get('name') if isinstance(p, dict) else p for p in parts]
                header.append("Location: " + ", ".join(p for p in parts if p))
        if job.get('validThrough'):
            header.append(f"Closing date: {job['validThrough']}")

        if job.get('description'):
            body = md.markdownify(job['description'])
        else:
            # favor_recall keeps bulleted lists, which the default settings drop on some sites
            body = trafilatura.extract(
                str(soup), output_format="markdown", include_tables=True, favor_recall=True
            )
        if not body:
            return ""
        return "\n".join(header + ["", body]).strip()

    def _get_link(self, soup: BeautifulSoup):
        page_url = self._get_page_url(soup)
        for anchor in soup.find_all('a', href=True):
            if re.search(r'\bapply\b', anchor.get_text(" ", strip=True), re.IGNORECASE):
                href = urljoin(page_url, anchor['href']) if page_url else anchor['href']
                if href.startswith('http'):
                    return href
        # No apply button (e.g. expired postings): fall back to the posting itself
        return page_url

    def _get_date_posted(self, soup: BeautifulSoup):
        job = get_job_posting_ld(soup)
        if job and job.get('datePosted'):
            return to_iso_date(job['datePosted'])
        label = r'date posted|posted on|placed on|date published|published on|date listed|listed on'
        return to_iso_date(find_labelled_value(soup, label))

    def _get_csv_path(self):
        return "data/miscjobs.csv"

def main():
    parser = ChronicleVitaeParser(html_dir="data/html_cache/cv")
    df = parser.extract_all_from_dir()
    df.to_csv('data/output.csv', index=False)

if __name__ == "__main__":
    main()