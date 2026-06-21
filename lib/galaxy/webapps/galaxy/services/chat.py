"""Service layer for the GalaxyAI chat endpoints.

Hosts the logic shared by the blocking ``/api/chat`` and streaming
``/api/chat/stream`` controllers: agent-context assembly, exchange persistence,
and the SSE streaming generator. Keeping the queue/task/keepalive machinery here
mirrors :class:`EventsService.open_stream` and keeps the controllers thin.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from functools import partial
from typing import (
    Any,
    Optional,
)

import anyio

from galaxy.managers.agents import AgentService
from galaxy.managers.chat import ChatManager
from galaxy.managers.context import ProvidesUserContext
from galaxy.managers.markdown_util import ready_galaxy_markdown_for_export
from galaxy.managers.sse import (
    IsDisconnected,
    SSEEvent,
)
from galaxy.model import (
    Job,
    Page,
    User,
)
from galaxy.schema.agents import (
    AgentProgressEvent,
    AgentResponse,
)
from galaxy.schema.schema import (
    ChatPayload,
    ChatResponse,
)
from galaxy.security.idencoding import IdEncodingHelper
from galaxy.webapps.galaxy.services.base import ServiceBase

log = logging.getLogger(__name__)


class ChatService(ServiceBase):
    """Business logic for chat turns, shared by the blocking and streaming endpoints."""

    def __init__(
        self,
        security: IdEncodingHelper,
        chat_manager: ChatManager,
        agent_service: AgentService,
    ):
        super().__init__(security)
        self.chat_manager = chat_manager
        self.agent_service = agent_service

    async def build_full_context(
        self,
        trans: ProvidesUserContext,
        query_context: dict[str, Any],
        page_id: Optional[int],
        page_obj: Optional[Page],
        exchange_id: Optional[int],
        payload: Optional[ChatPayload],
    ) -> dict[str, Any]:
        """Assemble the agent context (page content, conversation history, entities).

        The streaming endpoint calls this *before* constructing its response so
        every DB read happens up front -- nothing here may run while the SSE body
        streams (the request session is released by ``GalaxyStreamingResponse``).
        """
        full_context: dict[str, Any] = query_context.copy() if query_context else {}

        # Export page content (encodes IDs) so the agent sees the same
        # text the editor has -- hashes and proposals match the client.
        if page_id:
            full_context["page_id"] = page_id
            if page_obj:
                full_context["history_id"] = page_obj.history_id
                if not full_context.get("history_id"):
                    session_history = getattr(trans, "history", None)
                    if session_history:
                        full_context["history_id"] = session_history.id
                        full_context["history_is_session"] = True
                if page_obj.latest_revision_id:
                    rev = page_obj.latest_revision
                    if rev and rev.content:
                        exported, _, _ = ready_galaxy_markdown_for_export(trans, rev.content)
                        full_context["page_content"] = exported
                    else:
                        full_context["page_content"] = ""
                else:
                    full_context["page_content"] = ""

        # DB is the source of truth for history; use structured pydantic-ai
        # format so the router passes it as ``message_history`` rather than
        # flattening into a text blob.
        if exchange_id:
            # One fetch yields both the history and whether the previous turn asked a
            # clarifying question -- when it did, the router re-includes that turn so it
            # can route this (otherwise elliptical) answer instead of withholding history.
            db_history, responding_to_clarification = await anyio.to_thread.run_sync(
                partial(self.chat_manager.get_routing_history, trans, exchange_id)
            )
            full_context["conversation_history"] = db_history or []
            full_context["responding_to_clarification"] = responding_to_clarification
        else:
            full_context["conversation_history"] = []

        if payload and payload.entity_context:
            full_context["entities"] = payload.entity_context.model_dump(exclude_none=True)

        return full_context

    async def persist_exchange(
        self,
        trans: ProvidesUserContext,
        job: Optional[Job],
        exchange_id: Optional[int],
        page_id: Optional[int],
        query_text: str,
        agent_type: str,
        response_text: str,
        agent_resp: Optional[AgentResponse],
    ) -> Optional[int]:
        """Persist the turn and return the exchange id (or None when not stored).

        Inserts new ``ChatExchange``/``ChatMessage`` rows and commits. Safe to run
        after ``GalaxyStreamingResponse`` has released the request session: it only
        writes fresh rows (no lazy ORM reads), so the request ``scoped_session``
        transparently re-opening a session for this final write is correct.
        """
        if job:
            exchange = await anyio.to_thread.run_sync(partial(self.chat_manager.create, trans, job.id, response_text))
            return exchange.id
        if not trans.user:
            return None
        # Store the resolved agent (the specialist that answered) rather than the
        # requested "auto", so chat history reflects which agent responded.
        stored_agent_type = agent_resp.agent_type if agent_resp else agent_type
        if exchange_id:
            conversation_data = {
                "query": query_text,
                "response": response_text,
                "agent_type": stored_agent_type,
                "agent_response": agent_resp.model_dump() if agent_resp else None,
            }
            message_content = json.dumps(conversation_data)
            await anyio.to_thread.run_sync(partial(self.chat_manager.add_message, trans, exchange_id, message_content))
            return exchange_id
        storable_result = {
            "response": response_text,
            "agent_response": agent_resp.model_dump() if agent_resp else None,
        }
        if page_id:
            exchange = await anyio.to_thread.run_sync(
                partial(
                    self.chat_manager.create_page_chat, trans, page_id, query_text, storable_result, stored_agent_type
                )
            )
            return exchange.id
        exchange = await anyio.to_thread.run_sync(
            partial(self.chat_manager.create_general_chat, trans, query_text, storable_result, stored_agent_type)
        )
        return exchange.id

    async def stream_turn(
        self,
        trans: ProvidesUserContext,
        user: User,
        query_text: str,
        agent_type: str,
        full_context: dict[str, Any],
        exchange_id: Optional[int],
        page_id: Optional[int],
        is_disconnected: IsDisconnected,
    ) -> AsyncIterator[str]:
        """Run an agent turn and stream it as SSE frames.

        Yields zero or more ``event: progress`` frames as the agent reports steps,
        then exactly one ``event: result`` carrying the same ``ChatResponse`` the
        blocking endpoint returns, then a terminal ``event: done``. ``full_context``
        must already be materialized by :meth:`build_full_context` -- the agent turn
        itself does no DB work, so the released request connection stays free until
        the final :meth:`persist_exchange` write.
        """
        start_time = time.time()
        queue: asyncio.Queue = asyncio.Queue()
        done_sentinel = object()

        async def emit(event: AgentProgressEvent) -> None:
            await queue.put(SSEEvent(event="progress", data=event.model_dump_json()))

        async def run_turn() -> None:
            try:
                agent_response = await self.agent_service.route_and_execute(
                    query=query_text,
                    trans=trans,
                    user=user,
                    context=full_context,
                    agent_type=agent_type,
                    progress_callback=emit,
                )
                new_exchange_id = await self.persist_exchange(
                    trans,
                    None,
                    exchange_id,
                    page_id,
                    query_text,
                    agent_type,
                    agent_response.content,
                    agent_response,
                )
                chat = ChatResponse(
                    response=agent_response.content,
                    error_code=0,
                    error_message="",
                    agent_response=agent_response,
                    exchange_id=new_exchange_id,
                    processing_time=time.time() - start_time,
                )
                await queue.put(SSEEvent(event="result", data=chat.model_dump_json()))
            except Exception as e:
                log.exception("Error in streaming chat turn: %s", e)
                chat = ChatResponse(
                    response="Sorry, there was an error processing your query. Please try again later.",
                    error_code=500,
                    error_message="Internal error",
                    processing_time=time.time() - start_time,
                )
                await queue.put(SSEEvent(event="result", data=chat.model_dump_json()))
            finally:
                await queue.put(done_sentinel)

        task = asyncio.ensure_future(run_turn())
        try:
            while True:
                if await is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Comment frame keeps proxies from closing an idle connection
                    # during a long model call with no progress event in between.
                    yield ": keepalive\n\n"
                    continue
                if item is done_sentinel:
                    break
                yield item.to_wire()
            yield SSEEvent(event="done", data="{}").to_wire()
        finally:
            if not task.done():
                task.cancel()
