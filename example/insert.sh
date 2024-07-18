#!/bin/bash

message="${1:-test}"
psql postgres://postgres:postgres@localhost:5432/postgres -c "INSERT INTO messages.message (message) VALUES ('[\"$message\"]'::JSONB);"
