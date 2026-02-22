import asyncio
import logging
from typing import Callable, Awaitable
from config import NODES
import rpc

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 0.05
READ_TIMEOUT    = 0.05


class Transport:
    """
    Async TCP transport.

    Each node runs one server (listen on its port).
    Outgoing calls: open short-lived connection → send JSON line → read response → close.
    """

    def __init__(
        self,
        node_id: int,
        on_request_vote:   Callable = None,
        on_append_entries: Callable = None,
    ):
        self.node_id = node_id
        self.host, self.port = NODES[node_id]
        self.on_request_vote   = on_request_vote
        self.on_append_entries = on_append_entries
        self._server = None

    # ── Server ────────────────────────────────────────────────────────────────

    async def start_server(self):
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
        )
        logger.info(f"[Node {self.node_id}] Listening on {self.host}:{self.port}")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
            if not raw:
                return
            msg = rpc.decode(raw.decode())
            response = await self._dispatch(msg)
            writer.write(rpc.encode(response))
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"[Node {self.node_id}] Connection error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, msg):
        if isinstance(msg, rpc.RequestVote):
            return await self.on_request_vote(msg)
        elif isinstance(msg, rpc.AppendEntries):
            return await self.on_append_entries(msg)
        raise ValueError(f"Unknown message type: {type(msg)}")

    # ── Client ────────────────────────────────────────────────────────────────

    async def _send(self, peer_id: int, msg) -> object:
        host, port = NODES[peer_id]
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            writer.write(rpc.encode(msg))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
            return rpc.decode(raw.decode())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def send_request_vote(self, peer_id: int, msg: rpc.RequestVote):
        return await self._send(peer_id, msg)

    async def send_append_entries(self, peer_id: int, msg: rpc.AppendEntries):
        return await self._send(peer_id, msg)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def stop(self):
        if self._server:
            self._server.close()
