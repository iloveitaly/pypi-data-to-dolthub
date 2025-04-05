export PATH := "/opt/homebrew/opt/sqlite/bin:" + env_var('PATH')

set unstable

setup:
	if [ "$(uname)" = "Darwin" ]; then \
		brew install dolt; \
	else \
		sudo bash -c 'curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | sudo bash'; \
	fi

	pip install sqlite3-to-mysql

update_dolt: download_latest_sqlite simplify_sqlite sqlite_to_dolt

download_latest_sqlite:
	LATEST_URL=$(curl -s https://api.github.com/repos/pypi-data/pypi-json-data/releases/latest | \
			jq -r '.assets[] | .browser_download_url') && \
	curl -L -o pypi_data.sqlite.gz "$LATEST_URL" && \
	gunzip pypi_data.sqlite.gz

simplify_sqlite:
	# by default, the database contains all versions from all time, but we really only care about the latest version.
	# this reduces the DB to have a single row per package.
	sqlite3 pypi_data.sqlite > one_row_per_package_for_sqlite.log < one_row_per_package_for_sqlite.sql

	# we don't need this url table
	sqlite3 pypi_data.sqlite "DROP TABLE urls;"

	# if we want to push a slimmed version of the sqlitedb in the future, this is helpful
	sqlite3 pypi_data.sqlite "VACUUM;"


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
