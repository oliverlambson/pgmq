import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

import asyncpg
import pydantic

logger = logging.getLogger("pgmq")


class DottableRecord(asyncpg.Record):
    """Required to access record fields as attributes for Pydantic model_validate."""

    def __getattr__(self, name):
        return self[name]


NewMessageNotifyPayload = int  # message.id

MessageStatus = (
    Literal["success"]
    | Literal["failed"]
    | Literal["rejected"]
    | Literal["lock_expired"]
)


class Message(pydantic.BaseModel):
    id: int  # SERIAL PRIMARY KEY,
    created_at: datetime  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message: pydantic.Json[Any]  # JSONB NOT NULL,
    lock_expires_at: datetime | None  # TIMESTAMP DEFAULT NULL


class MessageArchive(pydantic.BaseModel):
    id: int  # SERIAL PRIMARY KEY,
    created_at: datetime  # TIMESTAMP NOT NULL,
    archived_at: datetime  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message: pydantic.Json[Any]  # JSONB NOT NULL,
    result: MessageStatus  # message_status NOT NULL,
    handled_by: str  # VARCHAR(50) NOT NULL,
    details: str | None  # TEXT DEFAULT NULL


async def delete_message(
    connection: asyncpg.connection.Connection | asyncpg.pool.PoolConnectionProxy,
    id_: int,
) -> None:
    await connection.execute("DELETE FROM messages.message WHERE id = $1;", id_)


async def retrieve_message(
    connection: asyncpg.connection.Connection | asyncpg.pool.PoolConnectionProxy,
    id_: int,
    *,
    lock_duration: timedelta = timedelta(minutes=1),
) -> Message | None:
    """
    avoids race conditions by locking
    first to set lock_expires_at gets the message
    returns None if already locked
    """
    row = await connection.fetchrow(
        """
        UPDATE messages.message
        SET lock_expires_at = CURRENT_TIMESTAMP + $2
        WHERE
            id = $1
            AND (lock_expires_at IS NULL OR lock_expires_at < CURRENT_TIMESTAMP)
        RETURNING *;
        """,
        id_,
        lock_duration,
        record_class=DottableRecord,
    )
    if row is None:  # already locked
        return None
    logger.debug("row=%s", row)
    return Message.model_validate(row, from_attributes=True)


async def create_message_archive(
    connection: asyncpg.connection.Connection | asyncpg.pool.PoolConnectionProxy,
    message: Message,
    result: MessageStatus,
    handled_by: str,
    details: str | None,
) -> MessageArchive:
    row = await connection.fetchrow(
        """
        INSERT INTO messages.message_archive (
            created_at,
            message,
            result,
            handled_by,
            details
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5
        )
        RETURNING *;
        """,
        message.created_at,
        json.dumps(message.message),
        result,
        handled_by,
        details,
        record_class=DottableRecord,
    )
    assert row is not None, "No row created!"
    logger.debug("row=%s", row)
    return MessageArchive.model_validate(row, from_attributes=True)
