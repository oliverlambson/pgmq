import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Literal

import asyncpg

logger = logging.getLogger(__name__)


NewMessageNotifyPayload = int  # message.id

MessageStatus = (
    Literal["success"]
    | Literal["failed"]
    | Literal["rejected"]
    | Literal["lock_expired"]
)


class Message(asyncpg.Record):
    id: int  # SERIAL PRIMARY KEY,
    created_at: datetime  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message: str  # JSONB NOT NULL,
    lock_expires_at: datetime | None  # TIMESTAMP DEFAULT NULL


class MessageArchive(asyncpg.Record):
    id: int  # SERIAL PRIMARY KEY,
    created_at: datetime  # TIMESTAMP NOT NULL,
    archived_at: datetime  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    message: str  # JSONB NOT NULL,
    result: MessageStatus  # message_status NOT NULL,
    handled_by: str  # VARCHAR(50) NOT NULL,
    details: str | None  # TEXT DEFAULT NULL


def process_message(raw_message: str) -> tuple[str, str | None]:
    message = json.loads(raw_message)
    logger.info(f"message.message={message}")
    if not isinstance(message, list):
        return "rejected", "invalid message format"
    if len(message) == 0:
        return "rejected", "no message in list"
    instruction = message[0]
    match instruction:
        case "fail":
            return "failed", "explicit fail instruction received"
        case "reject":
            return "rejected", "explicit reject instruction received"
        case _:
            return "success", "fake work was done"


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
    row = await connection.fetchrow(
        """
        UPDATE messages.message
        SET lock_expires_at = CURRENT_TIMESTAMP + INTERVAL '1 minute'
        WHERE
            id = $1
            AND (lock_expires_at IS NULL OR lock_expires_at < CURRENT_TIMESTAMP)
        RETURNING *;
        """,
        id_,
        record_class=Message,
    )
    assert row is not None, "No message retrieved!"
    logger.debug(f"{row=}")
    logger.info(
        f"message.id={row.get("id")} retrieved: '{row.get("message")}' (lock_expires_at={row.get("lock_expires_at")})"
    )

    # do work
    raw_message = row.get("message")
    assert raw_message is not None, "Message is None!"
    assert isinstance(raw_message, str), "Message is not a string!"
    result, details = process_message(raw_message)
    logger.info(f"{result=}, {details=}")
    logger.info(f"message.id={row.get("id")} work complete")

    # mark message as handled
    async with connection.transaction():
        _ = await connection.execute("DELETE FROM messages.message WHERE id = $1;", id_)
        archive_row = await connection.fetchrow(
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
            row.get("created_at"),
            row.get("message"),
            result,
            "worker",
            details,
            record_class=MessageArchive,
        )
        assert archive_row is not None, "No archive row created!"
    logger.info(
        f"message.id={row.get("id")} archived to message_archive.id={archive_row.get("id")} (result='{archive_row.get("result")}')"
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
