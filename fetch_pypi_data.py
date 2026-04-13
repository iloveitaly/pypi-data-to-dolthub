import os
import pandas as pd
from google.cloud import bigquery
import sys
import json
from google.oauth2 import service_account

def fetch_data():
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        print("Error: GCP_PROJECT_ID environment variable is not set.")
        sys.exit(1)

    credentials_json = os.environ.get("GCP_CREDENTIALS")
    if credentials_json:
        print("Loading credentials from GCP_CREDENTIALS environment variable...")
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        client = bigquery.Client(project=project_id, credentials=credentials)
    elif os.path.exists("gcp_key.json"):
        print("Loading credentials from gcp_key.json file...")
        client = bigquery.Client.from_service_account_json("gcp_key.json")
    else:
        print("Loading credentials from default environment...")
        client = bigquery.Client(project=project_id)
    
    # Query to get the latest release metadata for every package
    query = """
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
    
    print(f"Running BigQuery query in project {project_id}...")
    try:
        # Using to_dataframe with progress bar if possible, but keep it simple for GHA
        df = client.query(query).to_dataframe()
        
        print(f"Fetched {len(df)} packages. Saving to Parquet...")
        df.to_parquet("pypi_metadata.parquet", index=False)
        print("Successfully saved data to pypi_metadata.parquet")
    except Exception as e:
        print(f"Error fetching data from BigQuery: {e}")
        sys.exit(1)

if __name__ == "__main__":
    fetch_data()
