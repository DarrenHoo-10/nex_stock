# -*- coding: utf-8 -*-
"""Hindsight recall provider (HTTP API, ``requests`` only).

Imported lazily by the registry, only when ``ASSISTANT_RECALL_PROVIDER=hindsight``.
Endpoints (Hindsight HTTP API 1.x, ``hindsight-docs/static/openapi.json``):

- ``POST   /v1/default/banks/{bank_id}/memories``               retain (``items[]``)
- ``POST   /v1/default/banks/{bank_id}/memories/recall``        recall
- ``DELETE /v1/default/banks/{bank_id}/documents/{document_id}`` forget
- ``GET    /health``                                            liveness

Authentication is an optional ``Authorization: Bearer <key>`` header.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

import requests

from src.agent.recall.base import (
    RecallError,
    RecallHealth,
    RecallItem,
    RecallQuery,
    RecallTurn,
)

logger = logging.getLogger(__name__)

HINDSIGHT_PROVIDER_ID = "hindsight"
# Retain/forget run in a background worker; they may legitimately take longer
# than the per-question recall budget.
BACKGROUND_TIMEOUT_FLOOR_S = 10.0
RECALL_MAX_TOKENS = 1024
RECALL_BUDGET = "low"
RETAIN_CONTEXT = "股票助手问答"


class HindsightRecallProvider:
    provider_id = HINDSIGHT_PROVIDER_ID

    def __init__(
        self,
        *,
        base_url: str,
        bank_id: str,
        api_key: Optional[str] = None,
        timeout_s: float = 2.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._bank_id = (bank_id or "").strip()
        self._api_key = (api_key or "").strip() or None
        self._timeout_s = float(timeout_s)
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._bank_id)

    # -- helpers ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _bank_url(self, suffix: str) -> str:
        return f"{self._base_url}/v1/default/banks/{quote(self._bank_id, safe='')}/{suffix}"

    def _request(self, method: str, url: str, *, timeout: float, json_body: Optional[dict] = None):
        if not self.configured:
            raise RecallError("Hindsight is not configured (HINDSIGHT_BASE_URL / HINDSIGHT_BANK_ID)")
        try:
            return self._session.request(
                method, url, headers=self._headers(), json=json_body, timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RecallError(f"Hindsight request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _tags(turn_tags: Sequence[str], extra: Sequence[str]) -> List[str]:
        merged: List[str] = []
        for tag in [*turn_tags, *extra]:
            if tag and tag not in merged:
                merged.append(tag)
        return merged

    # -- provider API ----------------------------------------------------

    def retain(self, turn: RecallTurn) -> None:
        date_text = turn.occurred_at.strftime("%Y-%m-%d")
        extra = [f"date:{date_text}"]
        metadata = {
            "session_id": str(turn.session_id),
            "message_id": str(turn.message_id),
            "date": date_text,
        }
        if turn.market_phase:
            extra.append(f"phase:{turn.market_phase}")
            metadata["market_phase"] = str(turn.market_phase)
        body = {
            "items": [{
                "content": f"用户: {turn.user_text}\n助手: {turn.assistant_text}",
                "timestamp": turn.occurred_at.isoformat(),
                "context": RETAIN_CONTEXT,
                "document_id": turn.document_id,
                "tags": self._tags(turn.tags, extra),
                "metadata": metadata,
            }],
            # Server-side fact extraction calls a model; do not wait for it.
            "async": True,
        }
        response = self._request(
            "POST", self._bank_url("memories"),
            timeout=max(self._timeout_s, BACKGROUND_TIMEOUT_FLOOR_S), json_body=body,
        )
        self._raise_for_status(response, "retain")

    def recall(self, query: RecallQuery) -> Sequence[RecallItem]:
        body: Dict[str, Any] = {
            "query": query.text,
            "budget": RECALL_BUDGET,
            "max_tokens": RECALL_MAX_TOKENS,
        }
        if query.tags:
            body["tags"] = list(query.tags)
            body["tags_match"] = "any"
        if query.now is not None:
            body["query_timestamp"] = query.now.isoformat()
        response = self._request(
            "POST", self._bank_url("memories/recall"), timeout=self._timeout_s, json_body=body,
        )
        self._raise_for_status(response, "recall")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RecallError("Hindsight recall returned invalid JSON") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        items: List[RecallItem] = []
        for row in results or []:
            if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                continue
            score = (row.get("scores") or {}).get("final") if isinstance(row.get("scores"), dict) else None
            items.append(RecallItem(
                text=str(row["text"]).strip(),
                date=self._item_date(row),
                document_id=row.get("document_id"),
                score=score if isinstance(score, (int, float)) else None,
            ))
        # Newest first, then cap: the answer should lean on recent context.
        items.sort(key=lambda item: item.date or "", reverse=True)
        return items[: max(1, int(query.limit))]

    def forget(self, document_ids: List[str]) -> None:
        failures = 0
        for document_id in document_ids:
            url = self._bank_url(f"documents/{quote(str(document_id), safe='')}")
            try:
                response = self._request(
                    "DELETE", url, timeout=max(self._timeout_s, BACKGROUND_TIMEOUT_FLOOR_S),
                )
                if response.status_code == 404:
                    continue  # already gone
                self._raise_for_status(response, "forget")
            except RecallError:
                failures += 1
                logger.warning("Hindsight forget failed for one document")
        if failures:
            raise RecallError(f"Hindsight forget failed for {failures} document(s)")

    def health(self) -> RecallHealth:
        if not self.configured:
            return RecallHealth(
                provider_id=HINDSIGHT_PROVIDER_ID, status="not_configured",
                message="HINDSIGHT_BASE_URL / HINDSIGHT_BANK_ID 未配置",
            )
        started = time.monotonic()
        try:
            response = self._request("GET", f"{self._base_url}/health", timeout=self._timeout_s)
            ok = 200 <= response.status_code < 300
            message = None if ok else f"HTTP {response.status_code}"
        except RecallError as exc:
            ok, message = False, str(exc)
        return RecallHealth(
            provider_id=HINDSIGHT_PROVIDER_ID,
            status="ok" if ok else "error",
            message=message,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _item_date(row: Dict[str, Any]) -> Optional[str]:
        for tag in row.get("tags") or []:
            if isinstance(tag, str) and tag.startswith("date:") and len(tag) >= 15:
                return tag[5:15]
        for key in ("mentioned_at", "occurred_start"):
            value = row.get(key)
            if isinstance(value, str) and len(value) >= 10:
                return value[:10]
        return None

    @staticmethod
    def _raise_for_status(response, action: str) -> None:
        if not 200 <= response.status_code < 300:
            raise RecallError(f"Hindsight {action} returned HTTP {response.status_code}")
