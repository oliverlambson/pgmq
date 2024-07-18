#!/bin/bash

psql postgres://postgres:postgres@localhost:5432/postgres -c "DELETE FROM messages.message; DELETE FROM messages.message_archive;"
