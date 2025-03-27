#!/bin/bash

# Get the latest release download URL for the sqlite.gz file
LATEST_URL=$(curl -s https://api.github.com/repos/pypi-data/pypi-json-data/releases/latest | 
    grep -o '"browser_download_url": "[^"]*sqlite.gz"' | 
    cut -d'"' -f4)

# Download the file
curl -L -o pypi_data.sqlite.gz "$LATEST_URL"

# Unzip the file
gunzip pypi_data.sqlite.gz

# Import into DoltHub (assuming dolt is installed and configured)
# Replace YOUR_DOLTHUB_REPO with your actual DoltHub repository name
#dolt sql -q "DROP DATABASE IF EXISTS pypi_data; CREATE DATABASE pypi_data;"
#dolt sql --database pypi_data < pypi_data.sqlite
