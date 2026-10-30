"""Merge the per-site job CSVs into data/alljobs.csv, combining postings listed on several sites.

Two rows are the same posting if
  - their application links match (ignoring tracking parameters, "www.", http/https, trailing "/"),
    unless the link is generic: a bare domain, or shared by different rows of one file
    (e.g. a university's careers page); or
  - their job titles and universities match after normalization: case, punctuation, "of/in/the/or",
    plurals and parentheticals are ignored, so "Assistant, Associate, or Full Professor" matches
    "Assistant/Associate/Full Professor", but "Macroeconomics" never matches "Microeconomics".
    A title match never puts two rows of the same file in one posting: one site listing the same
    title twice (e.g. several Princeton postdocs) usually means different jobs.

Merged fields take the first informative value (not empty / 'none' / '...' / 'Not Mentioned'),
with the longest `text` and the earliest `date_posted`. `sources` lists the merged rows as
<file>:<id>. Statuses ('...' unseen, n no, m maybe, y yes, 1 yes with priority, d applied):
  1. 'd' from any source wins, since having applied is a fact rather than a judgement;
  2. else a status chosen in the previous alljobs.csv, so triage and conflict resolutions made
     there survive a re-run (an unresolved 'm|n' left there is not a choice and is recomputed);
  3. else the per-site files' decided statuses: their common value, or all of them joined with
     '|' (e.g. 'm|n') when they disagree, for deciding by hand.

Usage (from econ-jobs/):  python merge.py [--verbose]
"""
import glob
import os
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd

from rankings import normalize

INPUT_PATTERN = "data/*jobs.csv"
OUTPUT_CSV = "data/alljobs.csv"
# Input files in priority order for picking field values; others follow alphabetically
SOURCE_ORDER = ["hedjobs", "joejobs", "ejmjobs", "cvjobs", "miscjobs"]

TRACKING_PARAMS = re.compile(r"^(utm_.*|source|linksource|ref|origin|src|trk|gh_src)$", re.IGNORECASE)
TITLE_STOPWORDS = {"of", "in", "the", "or", "and", "for", "a", "an", "at", "to"}
UNINFORMATIVE = {"", "none", "nan", "...", "not mentioned", "[]"}


def normalize_link(link) -> str | None:
    if not isinstance(link, str) or not link.strip():
        return None
    parts = urlparse(link.strip())
    if not parts.netloc:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/")
    return urlunparse(("https", host, path, "", query, ""))


def is_generic_link(link: str) -> bool:
    # A bare domain is a site or careers homepage, not one posting
    parts = urlparse(link)
    return not parts.path and not parts.query


def title_key(title) -> frozenset:
    if not isinstance(title, str):
        return frozenset()
    title = re.sub(r"\(s\)", "", title)  # "Professor(s)" before parentheticals are dropped
    title = title.replace("-", "")  # "Post-Doctoral" / "Postdoctoral", "Tenure-Track" / "Tenure Track"
    words = normalize(title).replace("tenure track", "tenuretrack").split()
    words = [w.removesuffix("s") if len(w) > 3 else w for w in words if w not in TITLE_STOPWORDS]
    return frozenset(words)


def is_informative(value) -> bool:
    return not pd.isna(value) and str(value).strip().lower() not in UNINFORMATIVE


def load_inputs() -> pd.DataFrame:
    paths = [p for p in glob.glob(INPUT_PATTERN) if os.path.abspath(p) != os.path.abspath(OUTPUT_CSV)]
    rank = {name: i for i, name in enumerate(SOURCE_ORDER)}
    paths.sort(key=lambda p: (rank.get(os.path.basename(p)[:-4], len(rank)), p))
    frames = []
    for priority, path in enumerate(paths):
        df = pd.read_csv(path, dtype={"id": str, "status": str})  # keep status "1" from becoming 1.0
        file_key = os.path.basename(path).removesuffix("jobs.csv")
        df["source"] = file_key + ":" + df["id"].astype(str)
        df["_file"] = file_key
        df["_priority"] = priority
        frames.append(df)
        print(f"{path}: {len(df)} rows")
    return pd.concat(frames, ignore_index=True)


class Groups:
    """Union-find over row indices, tracking which files each group already holds."""

    def __init__(self, files):
        self.parent = list(range(len(files)))
        self.files = [{f} for f in files]
        self.reason = [set() for _ in files]

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i, j, reason, allow_same_file=True):
        a, b = self.find(i), self.find(j)
        if a == b:
            return False
        if not allow_same_file and self.files[a] & self.files[b]:
            return False
        self.parent[b] = a
        self.files[a] |= self.files[b]
        self.reason[a] |= self.reason[b] | {reason}
        return True


def group_rows(rows: pd.DataFrame) -> Groups:
    groups = Groups(rows["_file"].tolist())

    # 1. Same application link
    rows["_link"] = rows["link"].map(normalize_link)
    titles_per_link = rows.groupby(["_file", "_link"])["_title"].transform("nunique")
    usable = rows["_link"].map(lambda l: isinstance(l, str) and not is_generic_link(l))
    # A link several different postings of one file point to is a careers page, not a posting
    usable &= titles_per_link.fillna(1) <= 1
    for _, members in rows[usable].groupby("_link"):
        first = members.index[0]
        for other in members.index[1:]:
            groups.union(first, other, "link")

    # 2. Same normalized title and university, at most one row per file
    keyed = rows[(rows["_title"].map(len) > 0) & (rows["_univ"] != "")]
    for _, members in keyed.groupby(["_univ", "_title"], sort=False):
        members = members.sort_values("_priority")
        for i in members.index:
            for j in members.index:
                if i < j:
                    groups.union(i, j, "title", allow_same_file=False)
    return groups


def merge_group(members: pd.DataFrame, columns, previous_status) -> dict:
    members = members.sort_values("_priority")
    record = {}
    for col in columns:
        values = [v for v in members[col] if is_informative(v)]
        if col == "text":
            record[col] = max(values, key=len) if values else members[col].iloc[0]
        elif col == "date_posted":
            dates = pd.to_datetime(pd.Series(values, dtype=object), errors="coerce", format="mixed", utc=True)
            record[col] = values[dates.argmin()] if dates.notna().any() else (values[0] if values else None)
        elif col == "status":
            chosen = members["source"].map(previous_status).dropna().tolist()
            record[col] = resolve_status(chosen, [str(v).strip() for v in values])
        else:
            record[col] = values[0] if values else members[col].iloc[0]
    record["sources"] = "; ".join(members["source"])
    return record


def resolve_status(chosen: list[str], site: list[str]) -> str:
    """Merged status from statuses chosen in the previous alljobs.csv and the per-site files'
    decided statuses (see the module docstring for the rules)."""
    if "d" in chosen or "d" in site:
        return "d"
    for pool in (chosen, site):
        pool = list(dict.fromkeys(pool))
        if pool:
            return "|".join(pool)
    return "..."


def load_previous_status() -> dict:
    """source -> status chosen in an earlier alljobs.csv, so triage done there survives a re-run.
    Unresolved conflicts ("m|n") were produced by this script, not chosen, so they are left out."""
    if not os.path.exists(OUTPUT_CSV):
        return {}
    previous = pd.read_csv(OUTPUT_CSV, dtype={"id": str, "status": str})
    if "sources" not in previous.columns:
        return {}
    status = {}
    for sources, value in zip(previous["sources"], previous["status"]):
        if is_informative(value) and "|" not in str(value) and isinstance(sources, str):
            for source in sources.split("; "):
                status[source] = str(value).strip()
    return status


def main(verbose=False):
    rows = load_inputs()
    columns = [c for c in rows.columns if not c.startswith("_") and c != "source"]
    rows["_title"] = rows["job_title"].map(title_key)
    rows["_univ"] = rows["university"].fillna("").map(normalize)

    groups = group_rows(rows)
    rows["_group"] = [groups.find(i) for i in rows.index]
    previous_status = load_previous_status().get

    merged = []
    for root, members in rows.groupby("_group", sort=False):
        merged.append(merge_group(members, columns, previous_status))
        if verbose and len(members) > 1 and "title" in groups.reason[root]:
            print(f"\n[title] {' + '.join(members['source'])}")
            for _, m in members.iterrows():
                print(f"    {m['job_title']}  |  {m['university']}")

    out = pd.DataFrame(merged)
    out = out[["id", "sources"] + [c for c in columns if c != "id"]]
    out.to_csv(OUTPUT_CSV, index=False)

    sizes = rows.groupby("_group").size()
    reasons = pd.Series({root: "+".join(sorted(groups.reason[root])) for root in sizes[sizes > 1].index})
    print(f"\n{len(rows)} rows -> {len(out)} postings in {OUTPUT_CSV}")
    print(f"{(sizes > 1).sum()} postings merged from several rows, by: {reasons.value_counts().to_dict()}")
    conflicts = out["status"].astype(str).str.contains("|", regex=False)
    if conflicts.any():
        print(f"{conflicts.sum()} postings have conflicting statuses (joined with '|'), e.g. "
              f"{out.loc[conflicts, 'status'].value_counts().head(5).to_dict()}")


if __name__ == "__main__":
    main(verbose="--verbose" in sys.argv)
