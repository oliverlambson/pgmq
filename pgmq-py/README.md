# Example usage of pgmq

```sh
# install deps
rye sync
source .venv/bin/activate

# run worker
example/worker.py

# new shell
example/publish-message.sh
```

to empty the message & archive table, run:

```sh
example/clean-message-tables.sh
```
