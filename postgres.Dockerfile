FROM postgres:16-bookworm

ENV LANG=en_US.utf8

# plugins
RUN apt-get update && \
  apt-get -y install postgresql-16-cron

# config
COPY ./postgresql.conf /usr/share/postgresql/postgresql.conf.sample

# init scripts (*.sh, *.sql)
COPY ./migrations/ /docker-entrypoint-initdb.d/
