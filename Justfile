export PATH := "/opt/homebrew/opt/sqlite/bin:" + env_var('PATH')

set unstable

setup: setup_dolt

setup_dolt:
	if [ "$(uname)" = "Darwin" ]; then \
		brew install dolt; \
	else \
		sudo bash -c 'curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | sudo bash'; \
	fi

update_dolt: build_db sqlite_to_dolt

# Prepare snapshot from BigQuery (or download latest release)
prepare_snapshot *args:
	scripts/prepare_snapshot.py {{args}}

# Decompress and validate prepared snapshot integrity against metadata
validate_snapshot gz="pypi_data.sqlite.gz" meta="pypi_data.sqlite.meta.json":
	gzip -dc {{gz}} > tmp_validate.sqlite
	scripts/validate_db.py tmp_validate.sqlite --meta {{meta}}
	rm tmp_validate.sqlite

# Bundle verified snapshot into server build context
bundle_snapshot *args:
	scripts/bundle_snapshot.py {{args}}

# Build, index, validate, and bundle the SQLite database snapshot
build_db: (prepare_snapshot "--refresh") (bundle_snapshot "--source-db" "pypi_data.sqlite" "--dest-dir" "server")

build_sqlite: build_db

# Generate a fixture SQLite database for local development and testing
fixture_db:
	scripts/build_search_index.py --create-fixture server/pypi_data.sqlite

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

	# Import SQLite to Dolt using sqlite3-to-mysql via uvx, exporting ONLY projects table
	uvx --from sqlite3-to-mysql sqlite3mysql --sqlite-file pypi_data.sqlite \
			--sqlite-tables projects \
			--mysql-database $(basename $PWD) \
			--mysql-user root \
			--mysql-password "" \
			--mysql-host localhost \
			--mysql-port 3306

	# quit dolt server
	kill $DOLT_PID
	sleep 5

	# Add indexes to Dolt
	dolt sql < scripts/sql/mysql_indexes.sql

	dolt docs upload README.md README.md
	dolt add dolt_docs

	dolt add projects
	dolt commit -m "pypi update"
	dolt push --force origin main

# Check Python code formatting and linting
lint:
	ruff check .
	ruff format --check .

# Automatically fix linting and formatting
lint-fix:
	ruff check --fix .
	ruff format .

# Run full server and index builder test suite
test:
	cd server && uv sync --locked && uv run pytest -v

test_server: test

# Benchmark server search performance locally
benchmark iterations="30" concurrency="5":
	cd server && uv run python benchmark_search.py --iterations {{iterations}} --concurrency {{concurrency}}

# Run container smoke test against running instance
smoke_test *args:
	scripts/smoke_test.py {{args}}

# Build docker image for the API server (bundles selected root snapshot first)
docker src_db="pypi_data.sqlite": (bundle_snapshot "--source-db" src_db "--dest-dir" "server")
	cd server && railpack build .
	docker tag server:latest pypi-api:latest

# Start container with healthcheck and wait for readiness
docker_up port="8000":
	PORT={{port}} docker compose up -d --wait --wait-timeout 120

# Stop Docker Compose container and delete volumes
docker_down:
	docker compose down -v

# Start container, smoke test endpoints, capture logs on failure, and clean up
test_container port="8080":
	PORT={{port}} docker compose up -d --wait --wait-timeout 120 || (docker compose logs && exit 1)
	just smoke_test --port {{port}} || (docker compose logs && just docker_down && exit 1)
	just docker_down

# Start the FastAPI server locally for development with auto-reload
dev_server:
	@echo "Starting dev server..."
	cd server && DB_PATH=pypi_data.sqlite uv run uvicorn main:app --reload

dev: dev_server

# Set repository metadata (description, homepage, topics) from pyproject.toml
github_repo_set_metadata:
	gh repo edit \
		--description "$(yq '.project.description' pyproject.toml)" \
		--homepage "$(yq '.project.urls.Repository' pyproject.toml)" \
		--add-topic "$(yq '.project.keywords | join(",")' pyproject.toml)"
