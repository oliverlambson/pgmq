# Example usage of pgmq

```sh
# install deps
rye sync
source .venv/bin/activate

# run worker
worker

# new shell
scripts/publish-message.sh
```

to empty the message & archive table, run:

```sh
scripts/clean-message-tables.sh
```
