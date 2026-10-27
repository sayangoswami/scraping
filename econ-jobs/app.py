#%%
import streamlit as st
import pandas as pd
import os
import re

CSV_FILE = "data/hedjobs.csv"  # can also be "data/cvjobs.csv", "data/ejmjobs.csv", "data/joejobs.csv"

# 1. Page Configuration (Wide Layout)
st.set_page_config(
    page_title="Data Editor App",
    layout="wide", # This uses the full screen width instead of a narrow central column
    initial_sidebar_state="expanded"
)

# 2. Load data from the CSV file
@st.cache_data
def load_data(file_path):
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    else:
        return None

# Initialize session state from CSV
if "df" not in st.session_state:
    st.session_state.df = load_data(CSV_FILE)

st.title("Interactive Data Editor & Persistor")

# Halt execution if the file does not exist
if st.session_state.df is None:
    st.error(f"⚠️ **File Not Found:** Could not locate `{CSV_FILE}`. Please place the file in the working directory and refresh the app.")
    st.stop()

# Create a clean copy to apply filters onto
filtered_df = st.session_state.df.copy()

# 3. Sidebar Filters
st.sidebar.header("Filter Options")

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
        mask = filtered_df.astype(str).apply(lambda x: x.str.contains(global_search, case=False, na=False)).any(axis=1)
        filtered_df = filtered_df[mask]

st.sidebar.markdown("---")
st.sidebar.subheader("Column Specific Filters")

# Loop through every column to create dynamic filters
for col in filtered_df.columns:
    # 1. Filters for Numeric Columns (Integer / Float)
    if pd.api.types.is_numeric_dtype(filtered_df[col]):
        min_val = float(st.session_state.df[col].min())
        max_val = float(st.session_state.df[col].max())

        # Only create a slider if there is a range to filter
        if min_val < max_val:
            selected_range = st.sidebar.slider(
                f"Filter {col}",
                min_value=min_val,
                max_value=max_val,
                value=(min_val, max_val)
            )
            filtered_df = filtered_df[(filtered_df[col] >= selected_range[0]) & (filtered_df[col] <= selected_range[1])]

    # 2. Filters for Text / Categorical Columns
    else:
        # If there are fewer than 20 unique items, use a multi-select dropdown
        unique_vals = filtered_df[col].dropna().unique()
        if len(unique_vals) <= 20:
            selected_vals = st.sidebar.multiselect(f"Select {col}", options=unique_vals, default=[])
            if selected_vals:
                filtered_df = filtered_df[filtered_df[col].isin(selected_vals)]
        # Otherwise, fall back to a text search input for that column
        else:
            col_search = st.sidebar.text_input(f"Search {col}", "")
            if col_search:
                filtered_df = filtered_df[filtered_df[col].astype(str).str.contains(col_search, case=False, na=False)]


# 4. Data Editing Section
st.subheader("Edit Your Data Below")
st.caption("💡 **Tip:** Click any column header to sort. Double-click cells to edit manually.")

# Display interactive data editor
edited_df = st.data_editor(
    filtered_df,
    # num_rows="dynamic", # Enables adding/deleting rows
    width='stretch',
    height=550
)

# Sync manual table edits straight into master session state immediately
if not edited_df.equals(filtered_df):
    st.session_state.df.update(edited_df)
    st.rerun()


# 5. NEW: Bulk Commands Control Panel
st.markdown("---")
st.subheader("🛠️ Bulk Operations Toolbar")
st.caption("These commands will be applied only to your currently filtered view rows.")

bulk_col1, bulk_col2, bulk_col3 = st.columns([2, 2, 1])

with bulk_col1:
    target_column = st.selectbox("1. Choose Target Column", options=st.session_state.df.columns)
with bulk_col2:
    bulk_action = st.selectbox("2. Choose Action", options=[
        "Set to specific value",
        "Regex Search & Replace",
        "Math: Multiply numbers",
        "Math: Add to numbers"
    ])

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


# 6. Save & Persistence Action Buttons
st.markdown("---")
col1, col2 = st.columns(2)

with col1:
    if st.button("💾 Permanent Save to Master CSV File", type="primary", use_container_width=True):
        st.session_state.df.to_csv(CSV_FILE, index=False)
        st.success(f"Changes permanently written to {CSV_FILE}!")
        st.cache_data.clear()

with col2:
    csv_download_data = edited_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Download Current View as CSV File",
        data=csv_download_data,
        file_name="filtered_data_export.csv",
        mime="text/csv",
        use_container_width=True
    )
