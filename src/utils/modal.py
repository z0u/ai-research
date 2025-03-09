import asyncio
from functools import wraps
import inspect
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, Literal, TypeAlias, TypeVar

import modal

T = TypeVar("T")


SyncHandler: TypeAlias = Callable[[T], None]
AsyncHandler: TypeAlias = Callable[[T], Awaitable[None]]
Handler: TypeAlias = SyncHandler[T] | AsyncHandler[T]


log = logging.getLogger(__name__)

# A single Queue can contain [...] up to 5,000 items.
# https://modal.com/docs/reference/modal.Queue
MAX_LEN = 5_000


@asynccontextmanager
async def send_batch_to(
    receive: Handler[list[T]],
    trailing_timeout: float | None = 5,
    errors: Literal["throw", "log"] = "log",
) -> AsyncGenerator[SyncHandler[list[T]]]:
    """
    Create a distributed producer-consumer pattern for batch processing with Modal.

    This async context manager sets up a distributed queue system where multiple
    producers can send batches of values to a single consumer function. The context
    yields a function that producers can call to send batches of values.

    Inside the context, a consumer task continuously reads batches from the queue
    and processes them using the provided `receive` function. The consumer will
    continue processing values until the context is exited and any trailing values
    are handled.

    Args:
        receive: A function that processes batches of values. Will be called
            with each batch of values as they become available. Can be either
            synchronous or asynchronous — it will be called appropriately based
            on its type.
        trailing_timeout: Number of seconds to wait for trailing messages after
            the context manager exits. If None, waits indefinitely.
        errors: How to handle errors in trailing message processing:
            - 'throw': Raises a TimeoutError if trailing message processing times out
            - 'log': Logs a warning if trailing message processing times out

    Yields:
        send: A function that accepts a list of values to send to the consumer.
            This function can be called from multiple distributed workers.

    Example:
        ```python
        async def process_batch(items: list[str]) -> None:
            print(f"Processing {len(items)} items")

        async with _send_batch_to(process_batch) as send_batch:
            # This can be called from multiple distributed workers
            send_batch(["item1", "item2", "item3"])
        ```

    """
    async with modal.Queue.ephemeral() as q:
        produce = _producer_batch(q)
        consume, stop = _batched_consumer(q, receive)

        log.debug("Starting consumer task")
        task = asyncio.create_task(consume())
        try:
            # The caller can send this to remote workers to put messages on the queue.
            yield produce
        finally:
            log.debug("Stopping consumer task")
            stop()
            try:
                await asyncio.wait_for(task, trailing_timeout)
            except TimeoutError as e:
                if errors == "throw":
                    e.add_note("While waiting for trailing messages")
                    raise e
                else:
                    log.warning("Timed out waiting for trailing messages")


def _producer_batch(q: modal.Queue):
    def produce_batch(values: list[T]) -> None:
        """Send values to the consumer."""
        # This function is yielded as the context, so there may be several
        # distributed producers. It gets pickled and sent to remote workers
        # for execution, so we can't use local synchronization mechanisms.
        # All we have is a distributed queue - but we can send signals on a
        # separate control partition of that queue. Using a control channel
        # avoids the need for polling and timeouts.

        # Emit values.
        q.put_many(values)

        # Notify consumer.
        q.put(True, partition="signal")

    return produce_batch


def _batched_consumer(q: modal.Queue, receive: Handler[list[T]]):
    areceive: AsyncHandler[list[T]] = corerce_to_async(receive)
    stop_event = asyncio.Event()

    async def batched_consume() -> None:
        """Take values from the queue until the context manager exits."""
        # This function is not exposed, so there's exactly one consumer.
        # It always runs locally.

        while True:
            # Wait until values are produced or the context manager exits.
            get_task = asyncio.create_task(q.get_many.aio(MAX_LEN, partition="signal"))
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                [get_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Either way, get all available messages.
            values: list[T] = await q.get_many.aio(MAX_LEN, block=False)
            if values:
                await areceive(values)

            # Can't just check stop_event.is_set here; we need to know
            # whether it was set before the last batch.
            if stop_task in done:
                await q.clear.aio(all=True)
                break

    def stop():
        """Stop the consumer."""
        stop_event.set()

    return batched_consume, stop


@asynccontextmanager
async def send_to(
    receive: Handler[T],
    trailing_timeout: float | None = 5,
    errors: Literal["throw", "log"] = "log",
) -> AsyncGenerator[SyncHandler[T]]:
    """
    Create a distributed producer-consumer pattern for single-item processing with Modal.

    Inside the context, a consumer task continuously reads items from the queue
    and processes them using the provided `receive` function. The consumer will
    continue processing values until the context is exited and any trailing values
    are handled.

    For batch processing, use `send_to.batch` which accepts and processes lists of items.

    Args:
        receive: A function that processes a single value. Will be called
            with each value as it becomes available. Can be either
            synchronous or asynchronous — it will be called appropriately based
            on its type.
        trailing_timeout: Number of seconds to wait for trailing messages after
            the context manager exits. If None, waits indefinitely.
        errors: How to handle errors in trailing message processing:
            - 'throw': Raises a TimeoutError if trailing message processing times out
            - 'log': Logs a warning if trailing message processing times out

    Yields:
        send: A function that accepts a single value to send to the consumer.
            This function can be called from multiple distributed workers.

    Example:
        ```python
        async def process_item(item: str) -> None:
            print(f"Processing {item}")

        async with send_to(process_item) as send:
            # This can be called from multiple distributed workers
            send("item1")

        # For batch processing, use send_to.batch instead
        async with send_to.batch(process_batch) as send_batch:
            send_batch(["item1", "item2", "item3"])
        ```

    """
    async with send_batch_to(
        receive=_consumer(receive=receive),
        trailing_timeout=trailing_timeout,
        errors=errors,
    ) as emit_batch:
        yield _producer(emit_batch)


def _producer(emit_batch: SyncHandler[list[T]]) -> SyncHandler[T]:
    def produce_one(value: T) -> None:
        """Send a value to the consumer."""
        emit_batch([value])

    return produce_one


def corerce_to_async(fn: Callable[..., T | Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    if inspect.iscoroutinefunction(fn):
        return fn

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def _consumer(receive: Handler[T]):
    areceive: AsyncHandler[T] = corerce_to_async(receive)

    async def consume_from_batch(values: list[T]) -> None:
        """Process a value."""
        for value in values:
            await areceive(value)

    return consume_from_batch


# class Pipe:
#     """
#     A builder for creating a pipe with multiple handlers.

#     This class allows you to add handlers that will be called when a message
#     is received.
#     """

#     def __init__(self, trailing_timeout: float = 5, errors: Literal["throw", "log"] = "log"):
#         self.handlers: list[tuple[str, Handler]] = []
#         self.trailing_timeout = trailing_timeout
#         self.errors = errors
#         self._ctx_manager = None
#         self._send_fn = None

#     async def __aenter__(self) -> "Pipe":
#         async def receive(data: tuple[str, T]) -> None:
#             event_type, value = data
#             for evt, handler in self.handlers:
#                 if event_type != evt:
#                     continue
#                 if inspect.iscoroutinefunction(handler):
#                     await handler(value)
#                 else:
#                     handler(value)

#         # Start the send_to context manager
#         self._ctx_manager = send_to(receive, trailing_timeout=self.trailing_timeout, errors=self.errors)
#         self._send_fn = await self._ctx_manager.__aenter__()

#         # Return self for method chaining
#         return self

#     async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
#         # Properly clean up the send_to context manager
#         if self._ctx_manager:
#             await self._ctx_manager.__aexit__(exc_type, exc_val, exc_tb)
#             self._ctx_manager = None
#             self._send_fn = None

#     def on(self, event_type: str, handler: Handler[T]) -> "Pipe":
#         """Add a handler to the pipe for a specific event type."""
#         self.handlers.append((event_type, handler))
#         return self

#     def send(self, event_type: str, value: T) -> None:
#         """Send a message with event_type to the pipe."""
#         if self._send_fn is None:
#             raise RuntimeError("Pipe context has not been entered or has already exited")
#         self._send_fn((event_type, value))


# def pipe(trailing_timeout: float = 5, errors: Literal["throw", "log"] = "log") -> Pipe:
#     """
#     Create a new PipeBuilder instance.

#     Args:
#         trailing_timeout: Number of seconds to wait for trailing messages after
#             the context manager exits. If None, waits indefinitely.
#         errors: How to handle errors in trailing message processing:
#             - 'throw': Raises a TimeoutError if trailing message processing times out
#             - 'log': Logs a warning if trailing message processing times out

#     Returns:
#         A new PipeBuilder instance.
#     """
#     return Pipe(trailing_timeout=trailing_timeout, errors=errors)


send_to.batch = send_batch_to


@asynccontextmanager
async def run(app: modal.App, trailing_timeout=10):
    """
    Run a Modal app and display its stdout stream.

    This differs from `modal.enable_output`, in that this function only shows logs from inside the container.

    Args:
        app: The Modal app to run.
        trailing_timeout: Number of seconds to wait for trailing logs after the app exits.
    """

    async def consume():
        async for output in app._logs.aio():
            if output == "Stopping app - local entrypoint completed.\n":
                # Consume this infrastructure message
                continue
            # Don't add newlines, because the output contains control characters
            print(output, end="")
            # No need to break: the loop should exit when the app is done

    # 1. Start the app
    # 2. Start consuming logs
    # 3. Yield control to the caller
    # 4. Wait for the logs to finish

    async with app.run():
        task = asyncio.create_task(consume())
        yield

    # Can't wait inside the context manager, because the app would still be running
    try:
        await asyncio.wait_for(task, timeout=trailing_timeout)
    except asyncio.TimeoutError as e:
        e.add_note("While waiting for trailing stdout")
        raise e
