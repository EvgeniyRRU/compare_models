#!/bin/sh
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE rent_bot_test;
    GRANT ALL PRIVILEGES ON DATABASE rent_bot_test TO "$POSTGRES_USER";
EOSQL
