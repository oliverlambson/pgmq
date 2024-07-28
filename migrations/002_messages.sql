CREATE SCHEMA IF NOT EXISTS messages;

-- defer permissions to 003_permissions.sql
-- GRANT USAGE ON SCHEMA messages TO postgres;

CREATE TABLE IF NOT EXISTS messages.message (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message JSONB NOT NULL,
    lock_expires_at TIMESTAMP DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_lock_expires_at ON messages.message (lock_expires_at);

CREATE TYPE message_status AS ENUM ('success', 'failed', 'rejected', 'lock_expired');

CREATE TABLE IF NOT EXISTS messages.message_archive (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message JSONB NOT NULL,
    result message_status NOT NULL,
    handled_by VARCHAR(50) NOT NULL,
    details TEXT DEFAULT NULL
);

CREATE OR REPLACE FUNCTION new_message_nofify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('new_message', NEW.id::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER new_message_trigger
AFTER INSERT ON messages.message
FOR EACH ROW
EXECUTE FUNCTION new_message_nofify();

CREATE OR REPLACE FUNCTION dead_message_nofify() RETURNS trigger AS $$
BEGIN
    RAISE WARNING 'Error: messages.message_archive.id=% has result=%', NEW.id, NEW.result;
    -- TODO: use this for alerting
    PERFORM pg_notify('dead_message', NEW.id::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dead_message_trigger
AFTER INSERT ON messages.message_archive
FOR EACH ROW
WHEN (NEW.result != 'success')
EXECUTE FUNCTION dead_message_nofify();
