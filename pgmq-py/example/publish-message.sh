#!/bin/bash

if ! command -v psql &>/dev/null; then
	echo "psql not found, install it to run this script."
	exit 1
fi

function help {
	echo "Inserts a new message into messages.message."
	echo ""
	echo "Usage:"
	echo "  $0 <fail/reject/timeout/raise/{any}>"
}

if [ -z "$1" ]; then
	help
	exit 1
fi

if [ "$1" == "--help" ]; then
	help
	exit 0
fi

message="$1"
psql postgres://postgres:postgres@localhost:5432/postgres -c \
	"INSERT INTO messages.message (message) VALUES ('[\"$message\"]'::JSONB);"
