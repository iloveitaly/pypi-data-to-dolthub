#!/bin/bash

# Get the latest release download URL for the sqlite.gz file
LATEST_URL=$(curl -s https://api.github.com/repos/pypi-data/pypi-json-data/releases/latest |
    grep -o '"browser_download_url": "[^"]*sqlite.gz"' |
    cut -d'"' -f4)

# Download the file
curl -L -o pypi_data.sqlite.gz "$LATEST_URL"

# Unzip the file
gunzip pypi_data.sqlite.gz

# Start Dolt server in background and save PID
dolt sql-server --host 0.0.0.0 --port 3306 &
DOLT_PID=$!

# Wait briefly to ensure server starts
sleep 2

# Import SQLite to Dolt using sqlite3-to-mysql
sqlite3mysql --sqlite-file pypi_data.sqlite \
    --mysql-database pypi_data \
    --mysql-user root \
    --mysql-password "" \
    --mysql-host localhost \
    --mysql-port 3306
# Import into DoltHub (assuming dolt is installed and configured)
# Replace YOUR_DOLTHUB_REPO with your actual DoltHub repository name
#dolt sql -q "DROP DATABASE IF EXISTS pypi_data; CREATE DATABASE pypi_data;"
#dolt sql --database pypi_data < pypi_data.sqlite
