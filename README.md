# PyPI Data to DoltHub

A tool to download, process, and publish PyPI package data to a DoltHub repository.

## Overview

This project downloads the latest SQLite database from the [pypi-data/pypi-json-data](https://github.com/pypi-data/pypi-json-data) repository, processes it to keep only the latest version of each package, and publishes the resulting data to DoltHub.

## Prerequisites

- [Dolt](https://github.com/dolthub/dolt)
- [Just](https://github.com/casey/just)
- Python with pip
- sqlite3
- sqlite3-to-mysql Python package

## Setup

Run the setup command to install Dolt and required Python packages:

```bash
just setup
```

This will:
- Install Dolt using Homebrew on macOS or the install script on other platforms
- Install the sqlite3-to-mysql Python package

## Updating the DoltHub Repository

To update the DoltHub repository with the latest PyPI data, run:

```bash
just update_dolt
```

This command performs the following steps:

1. **Download the latest SQLite database**:
   - Fetches the most recent SQLite database from the pypi-data/pypi-json-data GitHub releases
   - Decompresses the gzipped database file

2. **Simplify the SQLite database**:
   - Reduces the database to contain only one row per package (the latest version)
   - Removes the unnecessary `urls` table
   - Optimizes the database size with VACUUM

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

## Repository

The processed data is available on DoltHub at [iloveitaly/pypi](https://www.dolthub.com/repositories/iloveitaly/pypi).
