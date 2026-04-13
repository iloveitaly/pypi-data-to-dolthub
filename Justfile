export PATH := "/opt/homebrew/opt/sqlite/bin:" + env_var('PATH')

set unstable

setup:
	if [ "$(uname)" = "Darwin" ]; then \
		brew install dolt; \
	else \
		sudo bash -c 'curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | sudo bash'; \
	fi

	pip install sqlite3-to-mysql google-cloud-bigquery pyarrow pandas db-dtypes

update_dolt: build_sqlite sqlite_to_dolt

build_sqlite:
	# Fetching latest PyPI metadata from Google BigQuery
	python fetch_pypi_data.py

	# Aggregating data via DuckDB; requires local temp space for disk-spilling
	mkdir -p duckdb_temp
	duckdb < build_latest_sqlite.sql

	rm -rf duckdb_temp pypi_metadata.parquet


reset_dolt:
	rm -rf .dolt* || true
	dolt init
	dolt remote add origin iloveitaly/pypi

[script]
sqlite_to_dolt: reset_dolt
	# these are the defaults, but let's make them explicit since we are using them in sqlite3mysql
	dolt sql-server --host 0.0.0.0 --port 3306 &
	DOLT_PID=$!

	# Wait briefly to ensure server starts
	sleep 2

	# Import SQLite to Dolt using sqlite3-to-mysql
	sqlite3mysql --sqlite-file pypi_data.sqlite \
			--mysql-database $(basename $PWD) \
			--mysql-user root \
			--mysql-password "" \
			--mysql-host localhost \
			--mysql-port 3306

	# quit dolt server
	kill $DOLT_PID

	# Add indexes to both Dolt and the SQLite file
	dolt sql < mysql_indexes.sql
	sqlite3 pypi_data.sqlite "CREATE INDEX IF NOT EXISTS idx_name ON projects (name);"

	dolt docs upload README.md README.md
	dolt add dolt_docs

	dolt add projects
	dolt commit -m "pypi update"
	dolt push --force origin main
