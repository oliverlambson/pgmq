# pgmq - a Postgres Message Queue

A set of conventions to use a couple of tables and Postgres' `LISTEN`/`NOTIFY`
functionality as a persistent message queue.

We use a table as our queue, and every time we `INSERT` a new message into the table,
we send a `NOTIFY` to a channel. The `NOTIFY` includes the id of the inserted row.
Workers `LISTEN` to the channel and process the messages.

## Architecture

```mermaid
sequenceDiagram
    participant Publisher
    participant Worker
    participant AdhocWorker
    participant `messages.message`
    participant `messages.message_archive`
    participant Cron
    participant `new_message`
    participant `dead_message`

    Publisher->>`messages.message`: INSERT new message
    `messages.message`->>`new_message`: (insert trigger) NOTIFY with message id
    `messages.message_archive`->>`dead_message`: (insert trigger when result != "success") NOTIFY with message id
    Worker->>`new_message`: LISTEN
    `new_message`->>Worker: message id
    Worker->>`messages.message`: UPDATE ... RETURNING to lock message
    alt Successful lock
        Worker->>Worker: Process message
        alt
            Worker->>`messages.message`: DELETE message
            Worker->>`messages.message_archive`: INSERT message with status "success"
        else Invalid message
            Worker->>`messages.message`: DELETE message
            Worker->>`messages.message_archive`: INSERT message with status "rejected"
        else Error processing message
            Worker->>`messages.message`: DELETE message
            Worker->>`messages.message_archive`: INSERT message with status "failed"
        else Lock expired
            Worker->>`messages.message`: DELETE message
            Worker->>`messages.message_archive`: INSERT message with status "lock_expired"
        else Worker dies
            Cron->>Cron: (every minute) Check for messages with expired locks
            Cron->>`messages.message`: DELETE message
            Cron->>`messages.message_archive`: INSERT message with status "lock_expired"
        end
    else Failed to lock
        Worker->>Worker: Another worker is processing the message
    end

    Cron->>Cron: (every minute) Check for unprocessed messages
    Cron->>`new_message`: Re-send NOTIFY for unprocessed messages

    AdhocWorker->>`messages.message_archive`: Process messages with status != "success"
```

## Running the example implementation

```sh
# shell 1: postgres logs
docker compose up --build
# shell 2: worker to LISTEN for notifies
source pgmq-py/.venv/bin/activate
pgmq-py/example/worker.py
# shell 3: insert a message (sends NOTIFY)
pgmq-py/example/publish-message.sh test
# shell 4: psql interactive session to inspect tables
psql postgres://postgres:postgres@localhost:5432/postgres
postgres=# select * from messages.message;
postgres=# select * from messages.message_archive;
```

## FAQ

<details>
  <summary><strong>What if we have no workers?</strong></summary>
  No new messages will be processed, and no messages will be lost. They just sit in the
  queue. The notify will be re-sent every minute until a worker processes the message.
</details>

<details>
  <summary>
    <strong>
      What if our worker can't process the message (e.g., invalid message)?
    </strong>
  </summary>
  The worker should delete the message from the message table and write it to the
  <code>messages.message_archive</code> table with the status of
  <code>"rejected"</code>.
</details>

<details>
  <summary>
    <strong>What if our worker has an error processing the message?</strong>
  </summary>
  The worker should delete the message from the message table and write it to the
  <code>messages.message_archive</code> table with the status of <code>"failed"</code>.
</details>

<details>
  <summary>
    <strong>
      What if our worker is still processing the message after the set timeout?
    </strong>
  </summary>
  The worker should delete the message from the message table and write it to the
  <code>messages.message_archive</code> table with the status of
  <code>"lock_expired"</code>.
</details>

<details>
  <summary>
    <strong>What if our worker goes down while processing a message?</strong>
  </summary>
  In the <code>UPDATE ... RETURNING</code> statement used to retrieve the message, the
  worker sets a value for <code>lock_expires_at</code>. If the worker goes down, the
  lock will eventually expire, at which point it will be deleted from the message table
  and written to the archive table, marked as <code>"lock_expired"</code>.
</details>

<details>
  <summary><strong>What if we have multiple workers?</strong></summary>
  To process a new message, the worker does not <code>SELECT</code> the message from the
  table. It does an <code>UPDATE ... RETURNING</code>. If the worker successfully
  updates the row, it will be able to process the message. If the worker fails to update
  the row, another worker has already locked the message and is processing it.
</details>

<details>
  <summary>
    <strong>
      How do we handle unsuccessful messages in the message_archive table?
    </strong>
  </summary>

You will need to set up a worker to process messages that are in
<code>messages.message_archive</code> table and have a status != <code>"success"</code>.
(You could do this automatically by <code>LISTEN</code>ing to the
<code>dead_message</code> channel.)

A <code>WARNING</code> is also raised in the postgres logs on every dead message added
to <code>messages.message_archive</code>, so you can use that to trigger alerts in
your infrastructure. (Or you could <code>LISTEN</code> on the <code>dead_message</code>
channel, but that may be more brittle.)

</details>

<details>
  <summary><strong>Why not use <code>SELECT ... FOR UPDATE</code>?</strong></summary>
  We could use <code>SELECT ... FOR UPDATE [NOWAIT / SKIP LOCKED]</code> to lock the
  row. However, in long-running transactions we'd be holding the lock for the entire
  duration to process the message. Setting the <code>lock_expires_at</code> on the row
  using <code>UPDATE ... RETURNING</code> means we have short transactions and can
  easily detect when the lock has expired.
</details>

<details>
  <summary><strong>How do we do scheduled messages?</strong></summary>
  Use the <code>pg_cron</code> extension to schedule messages to be inserted into the 
  message table.
</details>

## Building a custom Postgres image

We need to install the `pg_cron` extension, which doesn't come with standard postgres
image. This is done by customising the database's configuration, and providing some
custom initialisation scripts.

### Configuration

We use this to allow `pg_cron` to be loaded, and add it to the `postgres` database.

> `/usr/share/postgresql/postgresql.conf.sample` is used to generate the
> `postgresql.conf` at runtime, so overwrite this (keeping the `.sample`) to customise.
> [(see the docs)](https://github.com/docker-library/docs/blob/master/postgres/README.md#database-configuration)

### Initialisation

We use this to:

- enable the `pg_cron` extension
- create the `messages` schema with the `message` and `message_archive` tables
- create the `NOTIFY` functions that trigger on `INSERT`s to the `message[_archive]`
  tables
- create the cron jobs to re-send `NOTIFY`s for unhandled messages, and to archive
  messages with expired locks

> After the entrypoint calls initdb to create the default postgres user and
> database, it will run any `*.sql` files, run any executable `*.sh` scripts, and
> source any non-executable `*.sh` scripts found in that directory to do further
> initialization before starting the service.
> [(see the docs)](https://github.com/docker-library/docs/blob/master/postgres/README.md#initialization-scripts)

## `TODO`

- [ ] docs
  - [x] README.md
  - [x] mermaid diagram
- [x] postgres
  - [x] postgres with pg_cron installed
  - [x] tables for messages and message archive
  - [x] notify to channel on insert into message
  - [x] notify to channel on insert into message_archive if unsuccessful
  - [x] handle timed out locks in-db (worker fails) - cron?
  - [ ] separate `message_dead` table
- [ ] example implementation in Python
  - [x] handle timed out locks in-app
  - [x] worker to listen for notifies and process messages
  - [ ] cron job to schedule message (insert into messages)
  - [ ] use a message schema
- [ ] python client library
- [ ] example implementation in Go?

---

## Design notes

> [!NOTE]
>
> These were earlier notes during development.

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

## Listen and Notify

```sql
LISTEN channel;
NOTIFY channel, 'message';
```

## Scheduled messages

`pg_cron` writes messages to message table. Then handled just like a normal
message. i.e., `pg_cron` is another message source and there's nothing special
about it.
