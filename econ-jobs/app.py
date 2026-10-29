#%%
import streamlit as st
import pandas as pd
import datetime as dt
import os
import re

import rankings

# 1. Page Configuration (Wide Layout)
st.set_page_config(
    page_title="Interactive Data Editor",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Available Files Configuration
DATASET_OPTIONS = {
    "HigherEdJobs (hedjobs.csv)": "data/hedjobs.csv",
    "Chronicle Vitae (cvjobs.csv)": "data/cvjobs.csv",
    "Econ Job Market (ejmjobs.csv)": "data/ejmjobs.csv",
    "JOE Jobs (joejobs.csv)": "data/joejobs.csv",
}

# Columns that get date-range filters instead of text filters (when present)
DATE_COLUMNS = ["date_posted", "review_start", "deadline"]
# Read-only columns computed by rankings.py from each posting's university; not saved to the CSV
RANK_COLS = ["rank_flag", "rank_reason", "carnegie", "qs_rank", "times_rank", "matched_name", "match_score"]
DATE_PRESETS = ["Any", "Today onward", "Next 7 days", "Next 30 days", "Past 7 days", "Past 30 days", "Custom range"]

# 2. Data Loading Function
@st.cache_data
def load_data(file_path: str):
    if os.path.exists(file_path):
        try:
            return pd.read_csv(file_path, dtype={"id": str})
        except Exception as err:
            st.error(f"Error parsing `{file_path}`: {err}")
            return None
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


def invalidate_view():
    # Forces the editor to be rebuilt from the master dataframe on the next run,
    # and stops the old editor's edits from being re-applied on top of newer changes
    st.session_state.pop("view_cols", None)
    st.session_state.pop("view_base", None)

# 3. Sidebar: Dataset Selection & Refresh Controls
st.sidebar.header("📁 Dataset Selection")

selected_label = st.sidebar.selectbox(
    "Choose Data Source",
    options=list(DATASET_OPTIONS.keys()),
    index=0
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
date_cols = [c for c in DATE_COLUMNS if c in master_df.columns]
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
                filtered_df = filtered_df[
                    (filtered_df[col] >= selected_range[0]) &
                    (filtered_df[col] <= selected_range[1])
                ]

    # Categorical / Text column filter
    else:
        unique_vals = sorted(master_df[col].dropna().unique())
        if 0 < len(unique_vals) <= 20:
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
col_info, col_reapply = st.columns([4, 1])
reapply_clicked = col_reapply.button("🔁 Re-apply Filters", use_container_width=True)

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
        f"Showing {len(view_base)} of {len(master_df)} rows. 💡 Double-click cells to edit; "
        f"rows stay in view until you change a filter or click Re-apply Filters."
    )

# The editor's input stays the same between edits; the edits themselves live in its widget state
editor_key = f"editor_{selected_file}_{st.session_state.editor_version}"
edited_df = st.data_editor(
    view_base,
    width="stretch",
    height=550,
    disabled=[c for c in RANK_COLS if c in view_base.columns],
    key=editor_key
)

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
