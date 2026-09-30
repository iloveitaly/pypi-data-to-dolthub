#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
#     "db-dtypes>=1.3.1",
#     "google-cloud-bigquery>=3.29.0",
#     "pandas>=2.2.3",
#     "pyarrow>=18.1.0",
# ]
# ///
"""
Fetch latest package metadata from Google BigQuery public dataset.
Exports one row per package (latest release by upload_time) to a Parquet file.
"""

import json
import os
import sys

import click
from google.cloud import bigquery
from google.oauth2 import service_account

# Output Parquet filename for PyPI metadata export
DEFAULT_OUTPUT_PARQUET = "pypi_metadata.parquet"

# BigQuery SQL query to extract latest metadata record per package
PYPI_LATEST_METADATA_QUERY = """
WITH ranked_metadata AS (
  SELECT 
    name,
    version,
    author,
    author_email,
    maintainer,
    maintainer_email,
    home_page,
    license,
    requires_python,
    summary,
    classifiers,
    requires_dist,
    upload_time,
    ROW_NUMBER() OVER(PARTITION BY name ORDER BY upload_time DESC) as rank
  FROM `bigquery-public-data.pypi.distribution_metadata`
)
SELECT
    name,
    version,
    author,
    author_email,
    maintainer,
    maintainer_email,
    home_page,
    license,
    requires_python,
    summary,
    classifiers,
    requires_dist,
    upload_time
FROM ranked_metadata
WHERE rank = 1
"""


def fetch_data(output_path: str = DEFAULT_OUTPUT_PARQUET):
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        click.echo("Error: GCP_PROJECT_ID environment variable is not set.", err=True)
        sys.exit(1)

    credentials_json = os.environ.get("GCP_CREDENTIALS")
    if credentials_json:
        click.echo("Loading credentials from GCP_CREDENTIALS environment variable...")
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        client = bigquery.Client(project=project_id, credentials=credentials)
    elif os.path.exists("gcp_key.json"):
        click.echo("Loading credentials from gcp_key.json file...")
        client = bigquery.Client.from_service_account_json("gcp_key.json")
    else:
        click.echo("Loading credentials from default environment...")
        client = bigquery.Client(project=project_id)

    click.echo(f"Running BigQuery query in project {project_id}...")
    try:
        df = client.query(PYPI_LATEST_METADATA_QUERY).to_dataframe()

        click.echo(f"Fetched {len(df)} packages. Saving to Parquet...")
        df.to_parquet(output_path, index=False)
        click.echo(f"Successfully saved data to {output_path}")
    except Exception as e:
        click.echo(f"Error fetching data from BigQuery: {e}", err=True)
        sys.exit(1)


@click.command()
@click.option(
    "--output",
    default=DEFAULT_OUTPUT_PARQUET,
    help="Output parquet file path.",
)
def cli(output: str):
    """Fetch latest PyPI release metadata from Google BigQuery public dataset."""
    fetch_data(output_path=output)


if __name__ == "__main__":
    cli()
