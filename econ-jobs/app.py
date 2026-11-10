#%%
import streamlit as st
import pandas as pd
import datetime as dt
import glob
import ast
import os
import re

import rankings

# 1. Page Configuration (Wide Layout)
st.set_page_config(
    page_title="Interactive Data Editor",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Available Files Configuration: every CSV in the chosen folder is offered (rescanned on each rerun),
# except rankings.py's reference tables. Known job files get a display name and come first.
DEFAULT_DATA_DIR = "data"
REFERENCE_FILES = {
    os.path.basename(p)
    for p in [rankings.CARNEGIE_CSV, rankings.CARNEGIE_LABELS_CSV, rankings.QS_CSV, rankings.TIMES_CSV]
}
DATASET_NAMES = {
    "alljobs.csv": "All sites, merged",
    "hedjobs.csv": "HigherEdJobs",
    "cvjobs.csv": "Chronicle Vitae",
    "ejmjobs.csv": "Econ Job Market",
    "joejobs.csv": "JOE Jobs",
    "miscjobs.csv": "Miscellaneous",
}


def find_datasets(folder: str):
    paths = [p for p in glob.glob(os.path.join(folder, "*.csv")) if os.path.basename(p) not in REFERENCE_FILES]
    order = list(DATASET_NAMES)
    paths.sort(key=lambda p: (order.index(os.path.basename(p)) if os.path.basename(p) in order else len(order), p))
    options = {}
    for path in paths:
        file_name = os.path.basename(path)
        name = DATASET_NAMES.get(file_name, file_name.removesuffix(".csv"))
        options[f"{name} ({file_name})"] = path
    return options


# Columns that always get date-range filters when present (job CSVs mix dates with free text
# like "ongoing"); any other column whose values are mostly ISO dates is detected as well
DATE_COLUMNS = ["date_posted", "review_start", "deadline"]
DATE_SHARE = 0.5
# Read-only columns computed by rankings.py from each posting's university; not saved to the CSV
RANK_COLS = ["rank_flag", "rank_reason", "carnegie", "qs_rank", "times_rank", "matched_name", "match_score"]
DATE_PRESETS = ["Any", "Today onward", "Next 7 days", "Next 30 days", "Past 7 days", "Past 30 days", "Custom range"]
# Fields shown in the popup's header rather than in its field list (when present)
HEADER_FIELDS = ["job_title", "university", "location", "link"]
# Columns a popup heading may come from, best first; otherwise the row's first short text value
TITLE_FIELDS = ["job_title", "position", "title", "name"]
# A `status` column gets a picker in the popup: these triage codes (see merge.py) when its values
# are all codes or `|`-joined codes, else its own distinct values if there are at most this many
MAX_PICKER_OPTIONS = 12
# A text column with at most this many distinct values (some repeated) gets a dropdown in the
# sidebar filters and in the popup's edit form
MAX_ENUM_OPTIONS = 20
STATUS_LABELS = {
    "...": "… unseen",
    "n": "n · no",
    "m": "m · maybe",
    "y": "y · yes",
    "1": "1 · priority",
    "d": "d · applied",
}
# Text longer than this gets its own section in the popup instead of a key/value line
LONG_TEXT = 80

# 2. Data Loading Function
@st.cache_data
def load_data(file_path: str):
    if os.path.exists(file_path):
        try:
            # status as str keeps the triage code "1" from becoming 1.0
            df = pd.read_csv(file_path, dtype={"id": str, "status": str})
        except Exception as err:
            st.error(f"Error parsing `{file_path}`: {err}")
            return None
        # Whole-number columns with blanks load as floats and would be saved back as "1.0";
        # the nullable Int64 type keeps them whole
        for col in df.columns:
            if pd.api.types.is_float_dtype(df[col]):
                values = df[col].dropna()
                if len(values) and (values == values.round()).all():
                    df[col] = df[col].astype("Int64")
        return df
    return None


@st.cache_data
def load_rank_info(postings: pd.DataFrame) -> pd.DataFrame:
    return rankings.classify(postings)


def add_rank_info(df: pd.DataFrame) -> pd.DataFrame:
    """Insert the computed ranking columns right after `university` (they are never saved)."""
    df = df.drop(columns=RANK_COLS, errors="ignore")
    if not {"university", "location"} <= set(df.columns):
        return df
    try:
        info = load_rank_info(df[["university", "location"]])
    except FileNotFoundError as err:
        st.sidebar.warning(f"Ranking columns unavailable: {err}")
        return df
    pos = df.columns.get_loc("university") + 1
    for offset, col in enumerate(RANK_COLS):
        df.insert(pos + offset, col, info[col])
    return df


def find_date_columns(df: pd.DataFrame) -> list:
    """DATE_COLUMNS that are present, plus text columns whose non-blank values are mostly ISO dates."""
    found = [c for c in DATE_COLUMNS if c in df.columns]
    for col in df.columns:
        if col in found or col in RANK_COLS or pd.api.types.is_numeric_dtype(df[col]):
            continue
        values = df[col].dropna().astype(str).str.strip()
        values = values[values != ""]
        if len(values) and values.str.match(r"\d{4}-\d{2}-\d{2}").mean() >= DATE_SHARE:
            found.append(col)
    return found


def parse_dates(series: pd.Series) -> pd.Series:
    # Only ISO-style values count as dates; free text ("ongoing", "mid-December") becomes NaT
    parsed = pd.to_datetime(series.astype(str), errors="coerce", format="ISO8601", utc=True)
    return parsed.dt.tz_localize(None).dt.normalize()


def preset_range(preset: str):
    today = pd.Timestamp(dt.date.today())
    return {
        "Today onward": (today, None),
        "Next 7 days": (today, today + pd.Timedelta(days=7)),
        "Next 30 days": (today, today + pd.Timedelta(days=30)),
        "Past 7 days": (today - pd.Timedelta(days=7), today),
        "Past 30 days": (today - pd.Timedelta(days=30), today),
    }[preset]


def as_list(value):
    """Return the items of a stringified Python list (e.g. "['CV', 'cover letter']"), else None."""
    if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
        try:
            items = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
        if isinstance(items, list):
            return [str(i) for i in items]
    return None


def is_blank(value) -> bool:
    return value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)) or str(value).strip() == ""


def close_details():
    st.session_state.pop("details_row", None)
    st.session_state.pop("details_edit", None)


def status_options(column: pd.Series):
    """(options, labels) for the popup's status picker, or None if the column has no small value set."""
    values = [str(v).strip() for v in column.dropna().unique() if str(v).strip()]
    if all(part in STATUS_LABELS for v in values for part in v.split("|")):
        return list(STATUS_LABELS), STATUS_LABELS
    if 0 < len(values) <= MAX_PICKER_OPTIONS:
        return sorted(values), {}
    return None


def status_picker(row_id, current, options, labels):
    """Status buttons in the popup. A pick is written to the master dataframe and the table is
    rebuilt to show it; the popup reopens on the rerun because `details_row` is still set."""
    current = "" if is_blank(current) else str(current).strip()
    st.markdown("**status**")
    picked = st.segmented_control(
        "status",
        options=options,
        format_func=lambda v: labels.get(v, v),
        default=current if current in options else None,
        label_visibility="collapsed",
        key=f"details_status_{row_id}",
    )
    if current and current not in options:
        st.caption(f"Current value `{current}` (e.g. a merge conflict); pick one to resolve it.")
    if picked is not None and picked != current:
        st.session_state.df.at[row_id, "status"] = picked
        invalidate_view()
        st.rerun()


def title_field(row: pd.Series):
    """Column to use as the popup heading: a TITLE_FIELDS column, else the first short text value."""
    for col in TITLE_FIELDS:
        if col in row.index and not is_blank(row[col]):
            return col
    for col, value in row.items():
        if col in RANK_COLS or is_blank(value) or not isinstance(value, str):
            continue
        if len(value) <= LONG_TEXT and as_list(value) is None:
            return col
    return None


def enum_values(column: pd.Series):
    """Sorted distinct values of a column that looks like a fixed set of choices, else None."""
    values = column.dropna().astype(str).str.strip()
    values = values[values != ""]
    distinct = sorted(values.unique())
    if 0 < len(distinct) <= MAX_ENUM_OPTIONS and len(distinct) < len(values):
        return distinct
    return None


def set_details_edit(on: bool):
    st.session_state.details_edit = on


def set_value(df: pd.DataFrame, row_id, col, value):
    """Write one edited value, making a plain int column nullable (Int64) when it gets a blank."""
    if value is None and df[col].dtype == "int64":
        df[col] = df[col].astype("Int64")
    df.at[row_id, col] = value


def edit_form(row_id):
    """Form with one field per column (rank columns are computed, so left out). Save writes the
    changed fields to the master dataframe and rebuilds the table; the popup stays open."""
    df = st.session_state.df
    row = df.loc[row_id]
    picker = status_options(df["status"]) if "status" in df.columns else None
    date_cols = find_date_columns(df)
    inputs = {}
    numbers = {}  # numeric column -> whether it holds whole numbers
    with st.form(f"edit_{row_id}", border=False):
        for col in df.columns:
            if col in RANK_COLS:
                continue
            value, key = row[col], f"edit_{row_id}_{col}"
            if col == "status" and picker:
                options, labels = picker
                current = "" if is_blank(value) else str(value).strip()
                inputs[col] = st.segmented_control(
                    col, options, format_func=lambda v: labels.get(v, v),
                    default=current if current in options else None, key=key,
                )
            elif pd.api.types.is_bool_dtype(df[col]):
                inputs[col] = st.checkbox(col, value=bool(value), key=key)
            elif pd.api.types.is_numeric_dtype(df[col]):
                # A text box rather than st.number_input, which can't be cleared back to blank
                numbers[col] = pd.api.types.is_integer_dtype(df[col])
                inputs[col] = st.text_input(
                    col, "" if pd.isna(value) else str(value), placeholder="(blank) — number", key=key,
                )
            elif (choices := enum_values(df[col])) and col not in date_cols:
                current = "" if is_blank(value) else str(value).strip()
                if current and current not in choices:
                    choices = sorted(choices + [current])
                inputs[col] = st.selectbox(
                    col, choices, index=choices.index(current) if current else None,
                    accept_new_options=True, placeholder="(blank) — pick or type a value", key=key,
                )
            else:
                text = "" if is_blank(value) else str(value)
                longest = df[col].dropna().astype(str).str.len().max()
                if (longest or 0) > LONG_TEXT or "\n" in text:
                    height = 300 if col == "text" else 120
                    inputs[col] = st.text_area(col, text, height=height, key=key)
                else:
                    inputs[col] = st.text_input(col, text, key=key)
        col_save, col_cancel = st.columns(2)
        save = col_save.form_submit_button("💾 Save", type="primary", use_container_width=True)
        col_cancel.form_submit_button("Cancel", use_container_width=True, on_click=set_details_edit, args=(False,))

    if save:
        bad = []
        for col, integer in numbers.items():
            text = inputs[col].strip()
            try:
                inputs[col] = None if text == "" else (int(text) if integer else float(text))
            except ValueError:
                bad.append(col)
        if bad:
            st.error(f"Not a valid number in: {', '.join(bad)}. Nothing was saved.")
            return
        for col, new in inputs.items():
            old = row[col]
            if isinstance(new, str):
                if (is_blank(old) and new.strip() == "") or (not is_blank(old) and str(old) == new):
                    continue
                new = new if new.strip() else None
            elif new is None and is_blank(old):
                continue
            elif new is not None and not is_blank(old) and new == old:
                continue
            set_value(df, row_id, col, new)
        st.session_state.pop("details_edit", None)
        invalidate_view()
        st.rerun()


@st.dialog("Details", width="large", on_dismiss=close_details)
def show_details(row_id):
    if st.session_state.get("details_edit"):
        edit_form(row_id)
        return
    df = st.session_state.df
    row = df.loc[row_id]
    heading = title_field(row)
    col_head, col_edit = st.columns([5, 1], vertical_alignment="center")
    col_head.markdown(f"### {row[heading] if heading else '(untitled)'}")
    col_edit.button("✏️ Edit", use_container_width=True, on_click=set_details_edit, args=(True,))
    where = " · ".join(str(row[c]) for c in ["university", "location"] if c in row and not is_blank(row[c]))
    if where:
        st.markdown(f"**{where}**")
    link = row.get("link")
    if not is_blank(link) and str(link).startswith("http"):
        st.link_button("Open application link ↗", str(link))
    skip = set(HEADER_FIELDS) | {heading}
    picker = status_options(df["status"]) if "status" in df.columns else None
    if picker:
        status_picker(row_id, row["status"], *picker)
        skip.add("status")

    short, long = [], []
    for col, value in row.items():
        if col in skip or is_blank(value):
            continue
        items = as_list(value)
        if items is not None or len(str(value)) > LONG_TEXT:
            long.append((col, items, value))
        else:
            short.append((col, value))

    if short:
        st.markdown("---")
        grid = st.columns(2)
        for i, (col, value) in enumerate(short):
            grid[i % 2].markdown(f"**{col}**: {value}")

    for col, items, value in long:
        st.markdown("---")
        st.markdown(f"**{col}**")
        if items is not None:
            st.markdown("\n".join(f"- {item}" for item in items) or "_(empty)_")
        elif col == "text":
            # The posting itself (markdown from parse.py); scroll it inside the popup
            with st.container(height=400):
                st.markdown(str(value))
        else:
            st.markdown(str(value))


def invalidate_view():
    # Forces the editor to be rebuilt from the master dataframe on the next run,
    # and stops the old editor's edits from being re-applied on top of newer changes
    st.session_state.pop("view_cols", None)
    st.session_state.pop("view_base", None)

# 3. Sidebar: Dataset Selection & Refresh Controls
st.sidebar.header("📁 Dataset Selection")

data_dir = st.sidebar.text_input(
    "Data folder",
    DEFAULT_DATA_DIR,
    key="data_dir",
    help="Every CSV in this folder is listed below. Relative paths are from where you ran streamlit.",
).strip() or DEFAULT_DATA_DIR
DATASET_OPTIONS = find_datasets(os.path.expanduser(data_dir))

if not DATASET_OPTIONS:
    st.warning(f"⚠️ No CSV files found in `{data_dir}`.")
    st.stop()

# Keyed so the current choice survives a new CSV appearing in the list
selected_label = st.sidebar.selectbox(
    "Choose Data Source",
    options=list(DATASET_OPTIONS.keys()),
    index=0,
    key="dataset"
)
selected_file = DATASET_OPTIONS[selected_label]

col_refresh, col_status = st.sidebar.columns([1, 1])
refresh_clicked = col_refresh.button("🔄 Refresh Data", use_container_width=True)

# 4. Session State & Schema Synchronization
# Reset and reload if switching datasets or user explicitly triggered a refresh
file_changed = st.session_state.get("current_file") != selected_file

if file_changed or refresh_clicked or ("df" not in st.session_state):
    if refresh_clicked:
        st.cache_data.clear()

    loaded_data = load_data(selected_file)
    st.session_state.df = add_rank_info(loaded_data) if loaded_data is not None else None
    st.session_state.current_file = selected_file
    st.session_state.pop("filter_sig", None)
    close_details()
    invalidate_view()
elif "view_base" in st.session_state:
    # Sync edits from the editor's widget state back to the master dataframe before filtering,
    # so filters see them. Idempotent: the editor keeps its full list of edits on its base view.
    editor_key = f"editor_{selected_file}_{st.session_state.editor_version}"
    edits = st.session_state.get(editor_key, {}).get("edited_rows", {})
    for row_pos, changes in edits.items():
        row_id = st.session_state.view_base.index[int(row_pos)]
        for col, value in changes.items():
            st.session_state.df.at[row_id, col] = value

st.title("Interactive Data Editor & Persistor")
st.caption(f"Active file: `{selected_file}`")

# Halt execution if the selected file is missing
if st.session_state.df is None:
    st.warning(f"⚠️ **File Not Found or Empty:** Could not load `{selected_file}`. Check if the file exists on disk.")
    st.stop()

master_df = st.session_state.df
filtered_df = master_df.copy()
# Every filter widget value goes in here; the view is only re-filtered when it changes
filter_state = []

# 5. Sidebar: Dynamic Filters (Adapts to Active Schema)
st.sidebar.markdown("---")
st.sidebar.header("🔍 Filter Options")

# Keyword search: comma-separated terms (Standard) or a single pattern (Regex)
search_mode = st.sidebar.radio("Search Type", ["Standard Text", "Regex Pattern"], horizontal=True)
global_search = st.sidebar.text_input(
    "Keyword Search",
    "",
    help="Standard: comma-separated keywords, e.g. `macro, labor`. Regex: one pattern.",
)
match_mode = st.sidebar.radio("Match", ["Any keyword", "All keywords"], horizontal=True)
search_cols = st.sidebar.multiselect(
    "Search in columns (empty = all)",
    options=list(master_df.columns),
    key=f"{selected_file}_search_cols"
)
filter_state += [search_mode, global_search, match_mode, search_cols]

if global_search:
    use_regex = search_mode == "Regex Pattern"
    terms = [global_search] if use_regex else [t.strip() for t in global_search.split(",") if t.strip()]
    haystack = filtered_df[search_cols or list(filtered_df.columns)].astype(str)
    try:
        term_masks = pd.concat(
            [
                haystack.apply(
                    lambda s, t=term: s.str.contains(t, regex=use_regex, flags=re.IGNORECASE, na=False)
                ).any(axis=1)
                for term in terms
            ],
            axis=1,
        )
        if not term_masks.empty:
            mask = term_masks.all(axis=1) if match_mode == "All keywords" else term_masks.any(axis=1)
            filtered_df = filtered_df[mask]
    except Exception as e:
        st.sidebar.error(f"Invalid Regex: {e}")

# Date filters: presets for quick ranges, or a custom range
date_cols = find_date_columns(master_df)
if date_cols:
    st.sidebar.markdown("---")
    st.sidebar.subheader("📅 Date Filters")

for col in date_cols:
    widget_key_prefix = f"{selected_file}_{col}"
    preset = st.sidebar.selectbox(f"{col}", DATE_PRESETS, key=f"{widget_key_prefix}_preset")
    filter_state.append(preset)
    if preset == "Any":
        continue

    all_dates = parse_dates(master_df[col])
    if preset == "Custom range":
        lo = all_dates.min().date() if all_dates.notna().any() else dt.date.today()
        hi = all_dates.max().date() if all_dates.notna().any() else dt.date.today()
        picked = st.sidebar.date_input(
            f"{col} range", value=(lo, hi), key=f"{widget_key_prefix}_range"
        )
        # The picker returns a single date while the second click is pending
        picked = tuple(picked) if isinstance(picked, (tuple, list)) else (picked,)
        start = pd.Timestamp(picked[0]) if picked else None
        end = pd.Timestamp(picked[1]) if len(picked) > 1 else None
    else:
        start, end = preset_range(preset)

    keep_undated = st.sidebar.checkbox(
        f"Keep rows without a date in {col} (e.g. 'ongoing')",
        value=True,
        key=f"{widget_key_prefix}_keep_undated"
    )
    filter_state += [str(start), str(end), keep_undated]

    dates = parse_dates(filtered_df[col])
    in_range = dates.notna()
    if start is not None:
        in_range &= dates >= start
    if end is not None:
        in_range &= dates <= end
    if keep_undated:
        in_range |= dates.isna()
    filtered_df = filtered_df[in_range]

st.sidebar.markdown("---")
st.sidebar.subheader("Column Specific Filters")

# Dynamically construct filters based on current dataframe columns.
# Options come from the full dataset so widgets don't change shape as other filters narrow rows.
for col in master_df.columns:
    if col in date_cols:
        continue
    widget_key_prefix = f"{selected_file}_{col}"

    # Numeric column filter
    if pd.api.types.is_numeric_dtype(master_df[col]):
        valid_vals = master_df[col].dropna()
        if not valid_vals.empty:
            min_val = float(valid_vals.min())
            max_val = float(valid_vals.max())
            if min_val < max_val:
                selected_range = st.sidebar.slider(
                    f"Filter {col}",
                    min_value=min_val,
                    max_value=max_val,
                    value=(min_val, max_val),
                    key=f"{widget_key_prefix}_slider"
                )
                filter_state.append(selected_range)
                # Untouched slider = no filter, so rows with a blank in this column aren't dropped
                if selected_range != (min_val, max_val):
                    filtered_df = filtered_df[
                        (filtered_df[col] >= selected_range[0]) &
                        (filtered_df[col] <= selected_range[1])
                    ]

    # Categorical / Text column filter
    else:
        unique_vals = sorted(master_df[col].dropna().unique())
        if 0 < len(unique_vals) <= MAX_ENUM_OPTIONS:
            # Keep current selections as options even if edits removed their last row
            ms_key = f"{widget_key_prefix}_multiselect"
            unique_vals = sorted(set(unique_vals) | set(st.session_state.get(ms_key, [])))
            selected_vals = st.sidebar.multiselect(
                f"Select {col}",
                options=unique_vals,
                default=[],
                key=ms_key
            )
            filter_state.append(selected_vals)
            if selected_vals:
                filtered_df = filtered_df[filtered_df[col].isin(selected_vals)]
        # Otherwise, fall back to a text search input for that column
        else:
            col_search = st.sidebar.text_input(f"Search {col}", "", key=f"{widget_key_prefix}_text")
            filter_state.append(col_search)
            if col_search:
                filtered_df = filtered_df[
                    filtered_df[col].astype(str).str.contains(col_search, case=False, na=False)
                ]

# Visible columns persist across edits and re-filtering
st.sidebar.markdown("---")
st.sidebar.subheader("👁️ Visible Columns")
chosen_cols = st.sidebar.multiselect(
    "Columns to show (empty = all)",
    options=list(master_df.columns),
    key=f"{selected_file}_visible_cols"
)
visible_cols = [c for c in master_df.columns if c in chosen_cols] or list(master_df.columns)

# 6. Data Table Display & In-place Editing
st.subheader("Edit Your Data Below")

# The view (which rows are shown) is frozen until the filters change, so editing a value
# that a filter depends on (e.g. status) doesn't make the row vanish or reset the table.
filter_sig = repr((selected_file, filter_state))
col_info, col_mode, col_reapply = st.columns([4, 1, 1], vertical_alignment="center")
grid_edit = col_mode.toggle(
    "✏️ Grid edit",
    key="grid_edit",
    help="Off: click any cell to open that row's details. On: edit cells directly in the table.",
)
reapply_clicked = col_reapply.button("🔁 Re-apply Filters", use_container_width=True)

# Switching modes rebuilds the table from the master dataframe (which already has the grid's edits)
if st.session_state.get("view_mode") != grid_edit:
    st.session_state.view_mode = grid_edit
    invalidate_view()

if st.session_state.get("filter_sig") != filter_sig or reapply_clicked:
    st.session_state.filter_sig = filter_sig
    st.session_state.view_ids = filtered_df.index
    invalidate_view()

if st.session_state.get("view_cols") != visible_cols:
    st.session_state.view_cols = visible_cols
    st.session_state.view_base = master_df.loc[st.session_state.view_ids, visible_cols]
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1

view_base = st.session_state.view_base
n_stale = len(view_base.index.difference(filtered_df.index))
n_new = len(filtered_df.index.difference(view_base.index))
if n_stale or n_new:
    col_info.caption(
        f"Showing {len(view_base)} rows. Your edits changed what the filters match "
        f"({n_stale} shown rows no longer match, {n_new} new rows match). "
        f"Click **Re-apply Filters** to update the view."
    )
else:
    col_info.caption(
        f"Showing {len(view_base)} of {len(master_df)} rows. 💡 "
        + ("Double-click cells to edit; " if grid_edit else "Click any cell to open its row; ")
        + "rows stay in view until you change a filter or click Re-apply Filters."
    )

# Fixed per file: a keyed st.dataframe keeps its identity when its data changes, so a sort set by
# clicking a header survives saves and status picks (a new key would reset it)
table_key = f"table_{selected_file}"
if grid_edit:
    # The editor's input stays the same between edits; the edits themselves live in its widget
    # state and are synced into the master dataframe at the top of each run.
    editor_key = f"editor_{selected_file}_{st.session_state.editor_version}"
    st.data_editor(
        view_base,
        width="stretch",
        height=550,
        disabled=[c for c in RANK_COLS if c in view_base.columns],
        key=editor_key
    )
else:
    event = st.dataframe(
        view_base,
        width="stretch",
        height=550,
        on_select="rerun",
        selection_mode="single-cell",
        key=table_key,
    )
    # Open the popup when the selection changes. Clicking the selected cell again deselects it,
    # which counts as a click on that row too, so the table never needs resetting (keeps scroll).
    cells = [tuple(c) for c in event.selection.get("cells", [])]
    prev_key, prev_cells = st.session_state.get("table_cells", (None, []))
    if prev_key != table_key:
        prev_cells = []
    st.session_state.table_cells = (table_key, cells)
    if cells != prev_cells and (cells or prev_cells):
        pos = (cells or prev_cells)[0][0]
        # A selection left over from before a filter change can point past the current rows
        if pos < len(view_base):
            st.session_state.details_row = view_base.index[pos]

# Stays open across reruns (e.g. after a status pick or a save) until dismissed
if st.session_state.get("details_row") in master_df.index:
    show_details(st.session_state.details_row)

# 7. Bulk Operations Toolbar (Dynamic Column Options)
st.markdown("---")
st.subheader("🛠️ Bulk Operations Toolbar")
st.caption("Applies changes to the rows currently shown in the table.")

bulk_col1, bulk_col2, bulk_col3 = st.columns([2, 2, 1])

with bulk_col1:
    target_column = st.selectbox(
        "1. Choose Target Column",
        options=[c for c in master_df.columns if c not in RANK_COLS],
        key=f"bulk_target_{selected_file}"
    )

with bulk_col2:
    bulk_action = st.selectbox(
        "2. Choose Action",
        options=[
            "Set to specific value",
            "Regex Search & Replace",
            "Math: Multiply numbers",
            "Math: Add to numbers"
        ],
        key=f"bulk_action_{selected_file}"
    )

with bulk_col3:
    st.write(" ") # Padding for button alignment
    execute_bulk = st.button("Run Bulk Operation", type="secondary", use_container_width=True)

# Generate conditional inputs based on action selected
if bulk_action == "Set to specific value":
    new_value = st.text_input("Enter the new value:", "")
elif bulk_action == "Regex Search & Replace":
    regex_find = st.text_input("Regex Find Pattern:", "")
    regex_replace = st.text_input("Replace with Text:", "")
elif "Math:" in bulk_action:
    math_factor = st.number_input("Enter numeric value:", value=1.0)

# Process Bulk updates when the button is clicked
if execute_bulk:
    affected_indices = view_base.index

    try:
        if bulk_action == "Set to specific value":
            master_df.loc[affected_indices, target_column] = new_value
            st.success("Successfully set values!")

        elif bulk_action == "Regex Search & Replace":
            master_df.loc[affected_indices, target_column] = (
                master_df.loc[affected_indices, target_column]
                .astype(str)
                .str.replace(regex_find, regex_replace, regex=True)
            )
            st.success("Regex transformation applied successfully!")

        elif "Math:" in bulk_action:
            if not pd.api.types.is_numeric_dtype(master_df[target_column]):
                st.error("Error: Target column is not numeric!")
            else:
                if "Multiply" in bulk_action:
                    master_df.loc[affected_indices, target_column] *= math_factor
                elif "Add" in bulk_action:
                    master_df.loc[affected_indices, target_column] += math_factor
                st.success("Mathematical update complete!")

        # Same rows stay in view; the table is rebuilt to show the new values
        invalidate_view()
        st.rerun()
    except Exception as error:
        st.error(f"Failed to execute command: {error}")

# 8. Save & Export Controls
st.markdown("---")
col_save, col_dl = st.columns(2)

with col_save:
    if st.button(f"💾 Save Changes to `{os.path.basename(selected_file)}`", type="primary", use_container_width=True):
        master_df.drop(columns=RANK_COLS, errors="ignore").to_csv(selected_file, index=False)
        st.success(f"Changes permanently written to `{selected_file}`!")
        st.cache_data.clear()

with col_dl:
    csv_download_data = master_df.loc[view_base.index].to_csv(index=False).encode("utf-8")
    st.download_button(
        label="📥 Download Current View as CSV",
        data=csv_download_data,
        file_name=f"export_{os.path.basename(selected_file)}",
        mime="text/csv",
        use_container_width=True
    )
