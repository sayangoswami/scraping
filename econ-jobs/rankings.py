"""Fuzzy-match job posting universities to Carnegie classifications and QS / Times rankings.

Only Carnegie is fuzzy-matched; QS and Times rows carry the same IPEDS id (`Cuid` = `unitid`),
so their ranks are looked up by the matched id. All three sources cover US institutions only.

Run directly (`python rankings.py`) to print the matches for every posting CSV for checking.
"""
import re
import unicodedata

import pandas as pd
from rapidfuzz import fuzz

CARNEGIE_CSV = "data/CarnegieClassification.csv"
CARNEGIE_LABELS_CSV = "data/CarnegieClassificationBasic2021.csv"
QS_CSV = "data/QSWorldRankings.csv"
TIMES_CSV = "data/TimesWorldRankings.csv"

# Carnegie basic2021 codes treated as research universities (R1 = very high, R2 = high)
RESEARCH_CODES = {15: "R1", 16: "R2"}
# Matches below these scores are treated as unmatched. When the posting's state is known only
# institutions in that state are considered, so a looser score (incl. substring matches) is safe;
# otherwise (mostly foreign postings) the name has to match almost exactly.
MIN_SCORE_IN_STATE = 95
MIN_SCORE_ANY_STATE = 97

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}
STATE_ABBRS = set(US_STATES.values())
US_COUNTRY = {"united states", "united states of america", "usa", "us", "u s a", "u s"}


# Applied to normalized names (both sources)
REPLACEMENTS = {
    r"\btechnological\b": "tech",  # "Tennessee Tech University"
    r"\bagricultural and technical\b": "a and t",  # "North Carolina A & T State University"
    r"\bagricultural and mechanical\b": "a and m",
    r"^uc\b": "university of california",
    r"^unc\b": "university of north carolina",
    r"^mit\b": "massachusetts institute of technology",
    r"^rutgers state university of new jersey\b": "rutgers university",
}
# Applied to posting names only: a school inside a university stands for the university
# ("Harvard Business School" -> "harvard university")
SCHOOL_SUFFIX_RE = re.compile(
    r"\b(graduate school of business|business school|school of business|kennedy school|"
    r"school of (management|law|public policy|public health|environment|engineering|economics))\b.*"
)
# Words after the university's own name: "indiana university kelley" (but not "university of chicago")
TRAILING_WORDS_RE = re.compile(r"^(.*?\b(university|college|institute))\b(?! of\b).*$")


def normalize(name: str) -> str:
    name = re.sub(r"[‐-―]", "-", name)  # en/em dashes, before they're dropped below
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # "José" -> "Jose"
    name = name.lower().replace("&", " and ")
    name = re.sub(r"\(.*?\)", " ", name)  # "(Caltech)"
    name = re.sub(r"\b(main campus|campus)\b", " ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = " ".join(w for w in name.split() if w not in {"the", "at"})
    for pattern, replacement in REPLACEMENTS.items():
        name = re.sub(pattern, replacement, name)
    return name


def normalize_posting(name: str) -> str:
    name = normalize(name)
    stripped = SCHOOL_SUFFIX_RE.sub("", name).strip()
    if stripped != name and stripped:
        if re.search(r"\b(university|college|institute)\b", stripped):
            name = TRAILING_WORDS_RE.sub(r"\1", stripped)
        else:
            name = stripped + " university"  # "yale school of management" -> "yale university"
    return name


def name_variants(name: str) -> list[str]:
    """Normalized forms of a Carnegie name a posting might use."""
    variants = {normalize(name), normalize(name.split("-")[0])}  # "University of Michigan-Ann Arbor"
    variants |= {re.sub(r"^(cuny|suny) ", "", v) for v in variants}  # "CUNY Bernard M Baruch College"
    variants |= {re.sub(r"^suny ", "state university of new york ", v) for v in variants}  # "SUNY College at Geneseo"
    return [v for v in variants if v]


def location_state(location) -> str | None:
    """Two-letter US state a posting's location ends with ("Kansas City, Missouri" -> "MO"),
    or None when it ends with anything else, e.g. a foreign country ("Milano, MI, ITALY")."""
    if not isinstance(location, str):
        return None
    location = re.sub(r"\(.*?\)", " ", location)  # "Swarthmore, PA (near Wilmington, DE)"
    for part in reversed(re.split(r",|\band\b", location)):
        part = " ".join(re.sub(r"[^A-Za-z ]", " ", part).split())  # drop zip codes and periods
        if not part or part.lower() in US_COUNTRY:
            continue
        if part.lower() in US_STATES:
            return US_STATES[part.lower()]
        if part in STATE_ABBRS:
            return part
        return None
    return None


def load_reference() -> pd.DataFrame:
    """One row per Carnegie institution with its classification and QS / Times ranks."""
    carnegie = pd.read_csv(CARNEGIE_CSV, usecols=["unitid", "name", "city", "stabbr", "basic2021"])
    labels = pd.read_csv(CARNEGIE_LABELS_CSV).set_index("id")["label"]
    carnegie["carnegie"] = carnegie["basic2021"].map(labels)

    qs = pd.read_csv(QS_CSV, usecols=["Cuid", "2024 RANK"], dtype=str)
    times = pd.read_csv(TIMES_CSV, usecols=["Cuid", "rank"], dtype=str)
    # A few Times rows (mostly medical schools, absent from Carnegie anyway) have Cuid "Not Found"
    qs["Cuid"] = pd.to_numeric(qs["Cuid"], errors="coerce")
    times["Cuid"] = pd.to_numeric(times["Cuid"], errors="coerce")
    qs = qs.dropna(subset=["Cuid"]).astype({"Cuid": int})
    times = times.dropna(subset=["Cuid"]).astype({"Cuid": int})

    ref = (
        carnegie
        .merge(qs.rename(columns={"Cuid": "unitid", "2024 RANK": "qs_rank"}), on="unitid", how="left")
        .merge(times.rename(columns={"Cuid": "unitid", "rank": "times_rank"}), on="unitid", how="left")
    )
    ref["norm"] = ref["name"].map(normalize)
    ref["variants"] = ref["name"].map(name_variants)
    ref["norm_city"] = ref["city"].fillna("").map(normalize)
    return ref


def match_university(university, location, ref: pd.DataFrame) -> pd.Series | None:
    """The Carnegie row best matching a posting, with its score, or None if nothing is close."""
    if not isinstance(university, str) or not university.strip():
        return None
    name = normalize_posting(university)
    state = location_state(location)
    exact = ref["variants"].map(lambda variants: name in variants)
    if state is None:
        candidates, min_score = ref, MIN_SCORE_ANY_STATE
    else:
        # Out-of-state rows only on an exact name: "Northeastern University" posting from Seattle
        candidates, min_score = ref[(ref["stabbr"] == state) | exact], MIN_SCORE_IN_STATE
    if candidates.empty:
        return None

    def score(row):
        best = max(fuzz.ratio(name, v) for v in row["variants"])
        if state is not None and len(name.split()) > 1:
            # One full name contained in the other: "Columbia University" / "...in the City of New York".
            # Not the campus-less variants: "University of California" is inside every UC campus.
            best = max(best, fuzz.partial_ratio(name, row["norm"]))
        return best

    scored = candidates.assign(match_score=candidates.apply(score, axis=1))
    scored = scored[scored["match_score"] >= min_score]
    if scored.empty:
        return None
    # Several rows can match equally well. Prefer an exact name, then the posting's state, then
    # its city ("University of Michigan" in Ann Arbor), then the most research-intensive campus
    # (lowest basic2021 code)
    norm_location = normalize(location) if isinstance(location, str) else ""
    scored = scored.assign(
        exact=exact[scored.index],
        in_state=scored["stabbr"] == state,
        in_city=scored["norm_city"].map(lambda city: bool(city) and city in norm_location),
        research=-scored["basic2021"],
    )
    sort_keys = ["match_score", "exact", "in_state", "in_city", "research"]
    return scored.sort_values(sort_keys, ascending=False).iloc[0]


def rank_flag(match) -> tuple[str, str]:
    """("skip" | "ok" | "no match", reasons) for a matched Carnegie row (or None)."""
    if match is None:
        return "no match", ""
    reasons = []
    if match["basic2021"] in RESEARCH_CODES:
        reasons.append(RESEARCH_CODES[match["basic2021"]])
    if pd.notna(match["qs_rank"]):
        reasons.append(f"QS {match['qs_rank']}")
    # Times lists unranked institutions as "Reporter"
    if pd.notna(match["times_rank"]) and match["times_rank"] != "Reporter":
        reasons.append(f"THE {match['times_rank']}")
    return ("skip" if reasons else "ok"), ", ".join(reasons)


def classify(df: pd.DataFrame, ref: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ranking columns for each posting's university, aligned with df's index."""
    ref = load_reference() if ref is None else ref
    rows = []
    for university, location in df[["university", "location"]].drop_duplicates().itertuples(index=False):
        match = match_university(university, location, ref)
        flag, reasons = rank_flag(match)
        rows.append({
            "university": university,
            "location": location,
            "rank_flag": flag,
            "rank_reason": reasons,
            "carnegie": None if match is None else match["carnegie"],
            "qs_rank": None if match is None else match["qs_rank"],
            "times_rank": None if match is None else match["times_rank"],
            "matched_name": None if match is None else match["name"],
            "match_score": None if match is None else round(match["match_score"]),
        })
    matches = pd.DataFrame(rows)
    out = df[["university", "location"]].merge(matches, on=["university", "location"], how="left")
    out.index = df.index
    return out.drop(columns=["university", "location"])


if __name__ == "__main__":
    pd.set_option("display.width", 250, "display.max_rows", None, "display.max_colwidth", 50)
    reference = load_reference()
    for path in ["data/hedjobs.csv", "data/cvjobs.csv", "data/joejobs.csv"]:
        jobs = pd.read_csv(path, dtype={"id": str})
        result = pd.concat([jobs[["university", "location"]], classify(jobs, reference)], axis=1)
        print(f"== {path}")
        print(result["rank_flag"].value_counts().to_string())
        cols = ["university", "location", "rank_flag", "rank_reason", "matched_name", "match_score"]
        print(result[cols].drop_duplicates(["university", "matched_name"]).to_string())
