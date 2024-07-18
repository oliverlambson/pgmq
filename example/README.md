# Example usage of pgmq

```sh
# install deps
rye sync
source .venv/bin/activate

# run worker
worker

# new shell
./insert.sh
```

to empty the message & archive table, run:

```sh
./clean.sh
```
