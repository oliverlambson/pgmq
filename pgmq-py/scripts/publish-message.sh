#!/bin/bash


if [ -z "$1" ]; then
    echo "Usage: $0 <message>"
    echo "  <message> = fail/reject/timeout/raise/{any}"
    exit 1
fi

message="$1"

psql postgres://postgres:postgres@localhost:5432/postgres -c \
  "INSERT INTO messages.message (message) VALUES ('[\"$message\"]'::JSONB);"
