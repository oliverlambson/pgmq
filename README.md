# Postgres Message Queue

> tldr;
>
> Write messages to a db table, emit notify on insert (with table name and primary key).
> Worker updates message in table with a lock times out at timestamp. Does work. On
> complete, deletes message from message table and writes it to archive table.

Postgres has a message-queue-like functionality with `NOTIFY` (publish) and `LISTEN`
(subscribe). It seems unnecessary to run a whole separate message queue broker if I only
have a couple simple use cases (namely, one or two workers listening to a queue where
the publishers are either a web server or cronjobs).

I don't really want my cronjobs for messages to run on webservers or workers, because
they're likely to have downtime since I will inevitably mess something up (i.e.,
unrelated bugs cause the server with the cronjob to go down, meaning the job's never
scheduled—that silent failure will be hard to catch). The database is already a critical
single-point of failure for whatever app, and it's probably the least likely to go down,
so why not run them there? Cron NOTIFYs/INSERTs into a table are simple enough that
running them on the db seems like minimal additional risk.

I also don't want my messages to disappear if a job fails or if a worker goes down or if
there are no workers at all. So my actual queue should be a persistent table of
messages. I want my workers to know when new messages are added to the table, so I can
send a `NOTIFY` on a channel that they `LISTEN` to after an insert has happened in the
message table.

I don't want multiple listening workers to process the same message, so I'll have a way
to lock messages. The first worker to successfully lock the message will do the work,
and if a worker tries to lock a message and fails, it won't be able to do the work.

After the message lock is acquired, it can reach a terminal state in a few ways:

- If the worker successfully does the work, it can delete the message from the message
  table and optionally persist the message and it's results to a message_archive table.
- If the worker hits an error, it can choose to either (1) delete the message from the
  message table and optionally persist the message and it's errors to a message_failures
  table; or (2) release the lock and let a worker have another go.
- If the worker falls over, the lock will expire. At that point the message should be
  either released back to the message queue or be dumped into the message_failures
  table.

todo:

- [x] postgres with pg_cron installed
- [x] tables for messages and message archive
- [x] notify to channel on insert into messages
- [ ] handle timed out locks (notify to different channel? move to dead letter table?)
- [x] worker to listen for notifies and process messages
- [ ] cron job to insert into messages
- [ ] message schema in example
- [ ] docs
- [ ] example in Go

## Listen and Notify

```sql
LISTEN channel;
NOTIFY channel, 'message';
```

## Scheduled messages

`pg_cron` writes messages to message table. Then handled just like a normal
message. i.e., `pg_cron` is another message source and there's nothing special
about it.

## Custom Postgres image

Need to install pg_cron which doesn't come with standard postgres image.

- Initialisation: https://github.com/docker-library/docs/blob/master/postgres/README.md#initialization-scripts

  > After the entrypoint calls initdb to create the default postgres user and
  > database, it will run any `*.sql` files, run any executable `*.sh` scripts, and
  > source any non-executable `*.sh` scripts found in that directory to do further
  > initialization before starting the service.

- Configuration: https://github.com/docker-library/docs/blob/master/postgres/README.md#database-configuration

  > `/usr/share/postgresql/postgresql.conf.sample` is used to generate the postgresql.conf
  > at runtime, so overwrite this (with .sample) to customise

## Running a message worker

```sh
# shell 1: postgres logs
docker compose up
# shell 2: worker to LISTEN for notifies
source example/.venv/bin/activate
worker
# shell 3: insert a message (sends NOTIFY)
example/insert.sh
# shell 4: psql interactive session to inspect tables
psql postgres://postgres:postgres@localhost:5432/postgres
postgres=# select * from messages.message;
postgres=# select * from messages.message_archive;
```
