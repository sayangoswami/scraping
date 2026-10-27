#%%
import streamlit as st
import pandas as pd
import os
import re
import glob

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

# 2. Data Loading Function
@st.cache_data
def load_data(file_path: str):
    if os.path.exists(file_path):
        try:
            return pd.read_csv(file_path)
        except Exception as err:
            st.error(f"Error parsing `{file_path}`: {err}")
            return None
    return None

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
    st.session_state.df = loaded_data.copy() if loaded_data is not None else None
    st.session_state.current_file = selected_file

st.title("Interactive Data Editor & Persistor")
st.caption(f"Active file: `{selected_file}`")

# Halt execution if the selected file is missing
if st.session_state.df is None:
    st.warning(f"⚠️ **File Not Found or Empty:** Could not load `{selected_file}`. Check if the file exists on disk.")
    st.stop()

# Work on a copy for filtering
filtered_df = st.session_state.df.copy()

# 5. Sidebar: Dynamic Filters (Adapts to Active Schema)
st.sidebar.markdown("---")
st.sidebar.header("🔍 Filter Options")

# NEW: Regex Global Search vs Standard Search Toggle
search_mode = st.sidebar.radio("Search Type", ["Standard Text", "Regex Pattern"], horizontal=True)
global_search = st.sidebar.text_input("Global Search (All Columns)", "")

if global_search:
    if search_mode == "Regex Pattern":
        try:
            # Match any row where any column value satisfies the regex pattern
            mask = filtered_df.astype(str).apply(
                lambda row: row.str.contains(global_search, regex=True, flags=re.IGNORECASE, na=False)
            ).any(axis=1)
            filtered_df = filtered_df[mask]
        except Exception as e:
            st.sidebar.error(f"Invalid Regex: {e}")
    else:
        mask = filtered_df.astype(str).apply(
            lambda x: x.str.contains(global_search, case=False, na=False)
        ).any(axis=1)
        filtered_df = filtered_df[mask]

st.sidebar.markdown("---")
st.sidebar.subheader("Column Specific Filters")

# Dynamically construct filters based on current dataframe columns
for col in filtered_df.columns:
    widget_key_prefix = f"{selected_file}_{col}"

    # Numeric column filter
    if pd.api.types.is_numeric_dtype(filtered_df[col]):
        valid_vals = st.session_state.df[col].dropna()
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
                filtered_df = filtered_df[
                    (filtered_df[col] >= selected_range[0]) &
                    (filtered_df[col] <= selected_range[1])
                ]

    # Categorical / Text column filter
    else:
        unique_vals = [v for v in filtered_df[col].dropna().unique()]
        if 0 < len(unique_vals) <= 20:
            selected_vals = st.sidebar.multiselect(
                f"Select {col}",
                options=unique_vals,
                default=[],
                key=f"{widget_key_prefix}_multiselect"
            )
            if selected_vals:
                filtered_df = filtered_df[filtered_df[col].isin(selected_vals)]
        # Otherwise, fall back to a text search input for that column
        else:
            col_search = st.sidebar.text_input(f"Search {col}", "", key=f"{widget_key_prefix}_text")
            if col_search:
                filtered_df = filtered_df[
                    filtered_df[col].astype(str).str.contains(col_search, case=False, na=False)
                ]

# 6. Data Table Display & In-place Editing
st.subheader("Edit Your Data Below")
st.caption("💡 **Tip:** Click column headers to sort. Double-click cells to edit manually.")

# Display interactive data editor
edited_df = st.data_editor(
    filtered_df,
    width="stretch",
    height=550,
    key=f"editor_{selected_file}"
)

# Sync edits back to master dataframe
if not edited_df.equals(filtered_df):
    st.session_state.df.update(edited_df)
    st.rerun()

# 7. Bulk Operations Toolbar (Dynamic Column Options)
st.markdown("---")
st.subheader("🛠️ Bulk Operations Toolbar")
st.caption("Applies changes to the currently filtered rows.")

bulk_col1, bulk_col2, bulk_col3 = st.columns([2, 2, 1])

with bulk_col1:
    target_column = st.selectbox(
        "1. Choose Target Column",
        options=list(st.session_state.df.columns),
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
    affected_indices = edited_df.index

    try:
        if bulk_action == "Set to specific value":
            st.session_state.df.loc[affected_indices, target_column] = new_value
            st.success("Successfully set values!")

        elif bulk_action == "Regex Search & Replace":
            st.session_state.df.loc[affected_indices, target_column] = (
                st.session_state.df.loc[affected_indices, target_column]
                .astype(str)
                .str.replace(regex_find, regex_replace, regex=True)
            )
            st.success("Regex transformation applied successfully!")

        elif "Math:" in bulk_action:
            if not pd.api.types.is_numeric_dtype(st.session_state.df[target_column]):
                st.error("Error: Target column is not numeric!")
            else:
                if "Multiply" in bulk_action:
                    st.session_state.df.loc[affected_indices, target_column] *= math_factor
                elif "Add" in bulk_action:
                    st.session_state.df.loc[affected_indices, target_column] += math_factor
                st.success("Mathematical update complete!")

        st.rerun() # Refresh layout views immediately
    except Exception as error:
        st.error(f"Failed to execute command: {error}")

# 8. Save & Export Controls
st.markdown("---")
col_save, col_dl = st.columns(2)

with col_save:
    if st.button(f"💾 Save Changes to `{os.path.basename(selected_file)}`", type="primary", use_container_width=True):
        st.session_state.df.to_csv(selected_file, index=False)
        st.success(f"Changes permanently written to `{selected_file}`!")
        st.cache_data.clear()

with col_dl:
    csv_download_data = edited_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="📥 Download Current View as CSV",
        data=csv_download_data,
        file_name=f"export_{os.path.basename(selected_file)}",
        mime="text/csv",
        use_container_width=True
    )
