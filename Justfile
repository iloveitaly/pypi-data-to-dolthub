export PATH := "/opt/homebrew/opt/sqlite/bin:" + env_var('PATH')

set unstable

setup:
	if [ "$(uname)" = "Darwin" ]; then \
		brew install dolt; \
	else \
		sudo bash -c 'curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | sudo bash'; \
	fi

	pip install sqlite3-to-mysql

update_dolt: build_sqlite sqlite_to_dolt

build_sqlite:
	# Clone the raw data repository (shallow and sparse)
	rm -rf pypi_json_data || true
	# blob:none filter avoids downloading all file contents initially
	git clone --depth 1 --filter=blob:none --sparse https://github.com/pypi-data/pypi-json-data pypi_json_data
	cd pypi_json_data && git sparse-checkout set release_data

	# Build the sqlite db using duckdb (points to local files)
	mkdir -p duckdb_temp
	duckdb < build_latest_sqlite.sql

	rm -rf pypi_json_data duckdb_temp


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

	rm pypi_data.sqlite

	# quit dolt server
	kill $DOLT_PID

	dolt sql < mysql_indexes.sql

	dolt docs upload README.md README.md
	dolt add dolt_docs

	dolt add projects
	dolt commit -m "pypi update"
	dolt push --force origin main
