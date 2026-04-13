# PyPI Data to DoltHub

[This repo](https://github.com/iloveitaly/pypi-data-to-dolthub) uses GitHub actions to download, process, and publish PyPI package data to a DoltHub repository.

The processed data is available on DoltHub at [iloveitaly/pypi](https://www.dolthub.com/repositories/iloveitaly/pypi).

## Overview

This project fetches the latest PyPI package metadata from the [Google BigQuery PyPI Public Dataset](https://console.cloud.google.com/marketplace/details/google_pypi/pypi), processes it to keep only the latest version of each package, and publishes the resulting data to DoltHub.

## Setup

### Prerequisites

1. **GCP Project**: You need a Google Cloud project with the BigQuery API enabled.
2. **Service Account**: Create a Service Account with the `BigQuery User` role.
3. **Authentication**:
   - For local development: `export GOOGLE_APPLICATION_CREDENTIALS="path/to/key.json"` and `export GCP_PROJECT_ID="your-project-id"`.
   - For GitHub Actions: Add `GCP_CREDENTIALS` (JSON key) to Secrets and `GCP_PROJECT_ID` to Variables.

#### Step-by-Step GCP Setup

1. **Create/Select a Project**:
   - Go to the [Google Cloud Console](https://console.cloud.google.com/).
   - Create a new project (or select an existing one). 
   - **Note your Project ID** (e.g., `my-pypi-sync-123`). This will be your `GCP_PROJECT_ID`.

2. **Enable BigQuery API**:
   - In the search bar at the top, search for **"BigQuery API"**.
   - Click **Enable** (if not already enabled).

3. **Create a Service Account**:
   - Navigate to **IAM & Admin** > **Service Accounts**.
   - Click **+ Create Service Account**.
   - Name it (e.g., `pypi-data-fetcher`) and click **Create and Continue**.
   - In the **Role** dropdown, search for and select **BigQuery User**. (This allows the script to run queries and bill them to your project).
   - Click **Continue** and then **Done**.

4. **Generate JSON Key**:
   - In the Service Accounts list, click on the email of the account you just created.
   - Go to the **Keys** tab.
   - Click **Add Key** > **Create new key**.
   - Select **JSON** and click **Create**. A `.json` file will be downloaded to your computer.

5. **Configure GitHub**:
   - Go to your repository on GitHub.
   - Navigate to **Settings** > **Secrets and variables** > **Actions**.
   - **Secret**: Click **New repository secret**. Name it `GCP_CREDENTIALS` and paste the **entire contents** of the downloaded JSON file.
   - **Variable**: Switch to the **Variables** tab and click **New repository variable**. Name it `GCP_PROJECT_ID` and paste your Project ID.

### Install Dependencies

Run the setup command to install Dolt and required Python packages:

```bash
just setup
```

## Updating the DoltHub Repository

To update the DoltHub repository with the latest PyPI data, run:

```bash
just update_dolt
```

This command performs the following steps:

1. **Fetch data from BigQuery**:
   - Runs `fetch_pypi_data.py` to query the `bigquery-public-data.pypi` dataset.
   - Joins distribution metadata with project metadata to get a comprehensive snapshot of the latest release for every package.
   - Saves the results to a local Parquet file.

2. **Generate SQLite database**:
   - Uses DuckDB to convert the Parquet metadata into a SQLite database (`pypi_data.sqlite`).
   - Ensures column types and formats match the Dolt schema.

3. **Reset and initialize Dolt**:
   - Initializes a fresh Dolt repository
   - Sets up the remote to point to `iloveitaly/pypi`

4. **Import SQLite data into Dolt**:
   - Starts a temporary Dolt SQL server
   - Imports data using sqlite3-to-mysql
   - Adds indexes to improve query performance
   - Commits and pushes the changes to the DoltHub repository

## SQL Operations

### Package Selection Logic

The script selects only the latest version of each package using a sophisticated version comparison algorithm in SQL. This handles semantic versioning correctly by:

1. Comparing the major version number
2. Comparing the minor version number
3. Comparing the patch version number
4. Comparing the build number (if any)

### Indexing

After importing to Dolt, the following index is created:

```sql
CREATE INDEX idx_name_prefix ON projects (name (20));
```

This index improves query performance by indexing the first 20 characters of package names.

## Database Structure

The resulting database contains a single `projects` table with detailed information about each Python package, including:

- Package name
- Latest version
- Summary
- License
- Author information
- Dependencies
- Project URLs
- And more metadata
