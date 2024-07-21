import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple

import asyncpg
import pydantic

logger = logging.getLogger(__name__)


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
    if row is None:
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


class ProcessMessageResult(NamedTuple):
    result: MessageStatus
    details: str | None


async def process_message(
    message: Any,
    *,
    timeout: timedelta,  # just for simulation in testing
) -> ProcessMessageResult:
    try:
        if not isinstance(message, list):
            return ProcessMessageResult("rejected", "invalid message format")
        if len(message) == 0:
            return ProcessMessageResult("rejected", "no message in list")
        instruction = message[0]
        match instruction:
            case "fail":
                return ProcessMessageResult(
                    "failed", "explicit fail instruction received"
                )
            case "reject":
                return ProcessMessageResult(
                    "rejected", "explicit reject instruction received"
                )
            case "timeout":
                await asyncio.sleep(timeout.total_seconds() + 1)
                raise NotImplementedError("Timeout should have fired, panic!")
            case "raise":
                raise ValueError("Explicit raise instruction received")
            case _:
                return ProcessMessageResult("success", "fake work was done")
    except asyncio.CancelledError:
        logger.info("process_message cancelled")
        raise


async def callback(
    connection: asyncpg.connection.Connection | asyncpg.pool.PoolConnectionProxy,
    pid: int,
    parameter: str,
    payload: object,
) -> None:
    """Adheres to asyncpg callback protocol

    connection: usable db conn
    pid: process id
    parameter: channel
    payload: message
    """
    timeout = timedelta(seconds=1)

    # recieve notification
    logger.debug(f"callback=<{connection=}, {pid=}, {parameter=}, {payload=}>")
    assert isinstance(payload, str), "Payload should always be a str!"
    assert parameter == "new_message", "This callback is only for 'new_message' channel"
    logger.info(f"notify received: new_message='{payload}'")
    try:
        id_ = NewMessageNotifyPayload(payload)
    except ValueError as e:
        raise ValueError(
            "Expected payload to contain only integer id for messages.message.id, got %s",
            payload,
        ) from e

    # grab the message
    # - avoid race conditions by using lock_expires_at
    message = await retrieve_message(connection, id_, lock_duration=timeout)
    if message is None:
        logger.info("Could not retrieve message (probably locked)")
        return
    logger.debug("message=%s", message)
    logger.info(
        f"{message.id=} retrieved: '{message.message}' ({message.lock_expires_at=})"
    )

    # do work
    assert message.message is not None, "Message is None!"
    assert message.lock_expires_at is not None, "lock_expires_at not set!"
    utcnow = datetime.now(UTC).replace(tzinfo=None)
    timeout_remaining = message.lock_expires_at - utcnow
    try:
        if timeout_remaining.total_seconds() < 0:
            logger.error(f"{timeout_remaining=}, {message.lock_expires_at=}, {utcnow=}")
            raise asyncio.TimeoutError("Lock expired before work could begin")
        async with asyncio.timeout(timeout_remaining.total_seconds()):
            process_result = await process_message(
                message.message, timeout=timeout_remaining
            )
        logger.info(f"{message.id=} work complete")
    except asyncio.TimeoutError as e:
        process_result = ProcessMessageResult("failed", "timed out")
        logger.error(f"{message.id=} timed out", exc_info=e)
    except Exception as e:
        process_result = ProcessMessageResult("failed", f"unhandled exception: {e}")
        logger.error(f"{message.id=} unhandled exception", exc_info=e)
    logger.debug(f"{process_result.result=}, {process_result=}")

    # mark message as handled
    async with connection.transaction():
        await delete_message(connection, message.id)
        message_archive = await create_message_archive(
            connection=connection,
            message=message,
            result=process_result.result,
            handled_by="worker",
            details=process_result.details,
        )
    logger.info(
        f"message.id={message.id} archived to {message_archive.id=} ({message_archive.result=})"
    )


async def main() -> None:
    logger.info("connecting to db")
    conn = await asyncpg.connect(
        "postgresql://postgres:postgres@localhost:5432/postgres"
    )
    logger.info("adding callback to LISTEN for new_message channel")
    await conn.add_listener(channel="new_message", callback=callback)
    logger.info("begin infinite loop")
    while True:
        await asyncio.sleep(1)


def entrypoint() -> None:
    debug = os.environ.get("DEBUG") == "1"
    logging.basicConfig(level=logging.INFO if not debug else logging.DEBUG)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("exiting on keyboard interrupt")
        sys.exit(0)


if __name__ == "__main__":
    entrypoint()
