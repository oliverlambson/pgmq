#!/bin/bash

if ! command -v psql &>/dev/null; then
	echo "psql not found, install it to run this script."
	exit 1
fi

function help {
	echo "Deletes all records from messages.message and messages.message_archive tables."
	echo ""
	echo "Usage:"
	echo "  $0"
}

if [ "$1" == "--help" ]; then
	help
	exit 0
fi

psql postgres://postgres:postgres@localhost:5432/postgres -c \
	"DELETE FROM messages.message; DELETE FROM messages.message_archive;"
