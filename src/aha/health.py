"""The in-pod HTTP server behind Kubernetes probes and Prometheus scrapes.

Three read-only endpoints, no authentication, so the bind address and the cluster's
NetworkPolicy are the access control (the default bind is loopback; the manifests widen it):

``GET /livez``
    The event loop answers and no critical background task has died for good. A failure
    here means the process is wedged, so a liveness probe should restart the pod.
``GET /readyz``
    Every shard this process owns is connected and ready, and no critical task has died or is
    still recovering from a crash. A failure here means "do not route to or roll past this pod
    yet", which gates a StatefulSet rollout. It is 503 until the first READY.
``GET /metrics``
    Prometheus text exposition of :class:`aha.metrics.Metrics`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from aha.bot import AhaBot

__all__ = ["HealthServer"]

log = logging.getLogger(__name__)


class HealthServer:
    """Serves ``/livez``, ``/readyz`` and ``/metrics`` for one bot."""

    def __init__(self, bot: AhaBot, host: str, port: int) -> None:
        self._bot = bot
        self._host = host
        self._port = port
        #: The port actually bound (differs from the requested one when that was 0).
        self.bound_port: int | None = None

    def app(self) -> web.Application:
        """Build the aiohttp application."""
        app = web.Application()
        app.add_routes(
            [
                web.get("/livez", self.livez),
                web.get("/readyz", self.readyz),
                web.get("/metrics", self.metrics),
            ]
        )
        return app

    async def run(self) -> None:
        """Serve until cancelled. Suitable as a :class:`aha.tasks.TaskRegistry` factory."""
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self._host, self._port)
            await site.start()
            # ``runner.addresses`` lists the bound sockets, which resolves port 0.
            self.bound_port = runner.addresses[0][1]
            log.info("Health server listening on %s:%s", self._host, self.bound_port)
            await asyncio.Event().wait()  # until cancelled
        finally:
            await runner.cleanup()

    async def livez(self, request: web.Request) -> web.Response:
        """Liveness: the loop answers and no critical task has died."""
        failed = sorted(self._bot.tasks.failed)
        if failed:
            return web.json_response({"status": "unhealthy", "failed_tasks": failed}, status=503)
        return web.json_response({"status": "ok"})

    async def readyz(self, request: web.Request) -> web.Response:
        """Readiness: every owned shard is ready and no critical task has died."""
        shards = self._bot.shard_states()
        failed = sorted(self._bot.tasks.failed)
        degraded = sorted(self._bot.tasks.degraded)
        ready = (
            bool(shards) and all(state.ready for state in shards) and not failed and not degraded
        )
        body = {
            "status": "ready" if ready else "not ready",
            "shards": [{"id": state.shard_id, "ready": state.ready} for state in shards],
            "failed_tasks": failed,
            "degraded_tasks": degraded,
        }
        return web.json_response(body, status=200 if ready else 503)

    async def metrics(self, request: web.Request) -> web.Response:
        """Prometheus exposition of the bot's registry."""
        return web.Response(
            body=generate_latest(self._bot.metrics.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )
