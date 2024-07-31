#!/usr/bin/env python

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, NoReturn

import asyncpg

from pgmq import (
    MessageStatus,
    NewMessageNotifyPayload,
    create_message_archive,
    delete_message,
    retrieve_message,
)

logger = logging.getLogger("pgmq.worker")


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
    message = await retrieve_message(connection, id_, lock_duration=timeout)
    if message is None:
        logger.info("could not retrieve message (probably locked)")
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


async def main() -> NoReturn:
    logger.info("[*] connecting to db")
    db_url = "postgresql://postgres:postgres@localhost:5432/postgres"
    conn = await asyncpg.connect(db_url)
    logger.info("[*] adding callback to LISTEN for new_message channel")
    await conn.add_listener(channel="new_message", callback=callback)
    logger.info("[*] waiting for messages")
    event = asyncio.Event()
    await event.wait()
    raise NotImplementedError("Finished main. Should never reach this point. (panic!)")


def entrypoint() -> None:
    debug = os.environ.get("DEBUG") == "1"
    logging.basicConfig(level=logging.INFO if not debug else logging.DEBUG)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("[*] exiting on keyboard interrupt")
        sys.exit(0)


if __name__ == "__main__":
    entrypoint()
