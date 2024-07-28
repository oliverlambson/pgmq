# pgmq - a Postgres Message Queue

The core idea is just a set of conventions to use a couple of tables and Postgres'
`LISTEN`/`NOTIFY` functionality in to create a message queue. We use a table as our queue,
and every time we `INSERT` a new message into the table, we send a `NOTIFY` to a channel.
The `NOTIFY` includes the id of the inserted row. Workers `LISTEN` to the channel and
process the messages.

## FAQ

### What if we have no workers?

No new messages will be processed, and no messages will be lost. They just sit in the
queue. The notify will be re-sent every minute until a worker processes the message.

### What if our worker can't process the message (e.g., invalid message)?

The worker should delete the message from the message table and write it to the
`messages.message_archive` table with the status of `"rejected"`.

### What if our worker has an error processing the message?

The worker should delete the message from the message table and write it to the
`messages.message_archive` table with the status of `"failed"`.

### What if our worker is still processing the message after the set timeout?

The worker should delete the message from the message table and write it to the
`messages.message_archive` table with the status of `"lock_expired"`.

### How do we handle unsuccessful messages in the message_archive table?

(You will need to set up a worker to process messages that are in
`messages.message_archive` table and have a status != `"success"`.)

### What if we have multiple workers?

To process a new message, the worker does not `SELECT` the message from the table. It
does an `UPDATE ... RETURNING`. If the worker successfully updates the row, it will be
able to process the message. If the worker fails to update the row, another worker has
already locked the message and is processing it.

### What if our worker goes down while processing a message?

In the `UPDATE ... RETURNING` statement used to retrieve the message, the worker sets a
value for `lock_expires_at`. If the worker goes down, the lock will eventually expire,
at which point it will be deleted from the message table and written to the archive
table, marked as `"lock_expired"`.

### Why not use `SELECT ... FOR UPDATE`?

We could use `SELECT ... FOR UPDATE [NOWAIT / SKIP LOCKED]` to lock the row. However,
in long-running transactions we'd be holding the lock for the entire duration to process
the message. Setting the `lock_expires_at` on the row using `UPDATE ... RETURNING` means
we have short transactions and can easily detect when the lock has expired.

### How do we do scheduled messages?

Use the `pg_cron` extension to schedule messages to be inserted into the message table.

---

## Design notes

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
- [x] handle timed out locks in-app
- [ ] handle timed out locks in-db (worker fails) - cron?
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
