"""Hindsight HTTP client (stdlib only).

Talks to the Hindsight API documented at https://hindsight.vectorize.io/docs. We only use four
endpoints; everything else (recall, reflect, retain) is exposed via thin wrappers.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import MemorialConfig

DEFAULT_TIMEOUT = 180.0


class HindsightAPIError(RuntimeError):
    """Raised when the Hindsight server returns a non-2xx response."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"Hindsight API {status} on {url}: {body[:500]}")
        self.status = status
        self.body = body
        self.url = url


@dataclass(frozen=True)
class HindsightClient:
    """Thin stdlib HTTP wrapper. Stateless; safe to construct per call."""

    base_url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "HindsightClient":
        """Construct from HINDSIGHT_API_URL / HINDSIGHT_API_KEY env vars (legacy path).

        Prefer `from_memorial_config()` in new code: it reads ~/.hindsight/claude-code.json
        and resolves bank ids automatically.
        """
        url = os.environ.get("HINDSIGHT_API_URL")
        if not url:
            raise RuntimeError("HINDSIGHT_API_URL is not set")
        return cls(
            base_url=url.rstrip("/"),
            api_key=os.environ.get("HINDSIGHT_API_KEY"),
            timeout=float(os.environ.get("HINDSIGHT_MEMORIAL_TIMEOUT", DEFAULT_TIMEOUT)),
        )

    @classmethod
    def from_memorial_config(cls, cfg: MemorialConfig) -> "HindsightClient":
        """Construct from a MemorialConfig (which knows URL + key).

        Used by the main entry point after loading config; falls back to env if config fields
        are empty.
        """
        url = cfg.api_url or os.environ.get("HINDSIGHT_API_URL")
        if not url:
            raise RuntimeError(
                "Hindsight API URL not configured: set HINDSIGHT_API_URL or write "
                "hindsightApiUrl into ~/.hindsight/claude-code.json"
            )
        return cls(
            base_url=url.rstrip("/"),
            api_key=cfg.api_key or os.environ.get("HINDSIGHT_API_KEY"),
            timeout=float(os.environ.get("HINDSIGHT_MEMORIAL_TIMEOUT", DEFAULT_TIMEOUT)),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise HindsightAPIError(e.code, err_body, url) from e
        except urllib.error.URLError as e:
            # Network-level failure (DNS, connection refused, timeout). Wrap it so callers
            # see a single typed exception and treat it the same as a server-side failure.
            raise HindsightAPIError(0, repr(e.reason), url) from e

    # ---- public API ----

    def reflect(
        self,
        bank_id: str,
        query: str,
        *,
        structured_output: dict[str, Any] | None = None,
        include_based_on: bool = True,
    ) -> dict[str, Any]:
        """POST /v1/default/banks/{bank_id}/reflect

        If `structured_output` is given, it is passed through as the response schema and the returned
        `structured_output` field is expected to be a JSON object conforming to it.
        """
        body: dict[str, Any] = {"query": query, "include_based_on": include_based_on}
        if structured_output is not None:
            body["response_schema"] = structured_output
        return self._request(
            "POST",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/reflect",
            body=body,
        )

    def get_memory(self, bank_id: str, memory_id: str) -> dict[str, Any]:
        """GET /v1/default/banks/{bank_id}/memories/{memory_id}"""
        return self._request(
            "GET",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/{urllib.parse.quote(memory_id, safe='')}",
        )

    def update_memory(
        self,
        bank_id: str,
        memory_id: str,
        *,
        state: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /v1/default/banks/{bank_id}/memories/{memory_id}

        Only `state` and `reason` are exposed; other curate fields are not relevant to memorial.
        """
        patch: dict[str, Any] = {}
        if state is not None:
            patch["state"] = state
        if reason is not None:
            patch["reason"] = reason
        return self._request(
            "PATCH",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/{urllib.parse.quote(memory_id, safe='')}",
            body=patch,
        )

    def clear_memory_observations(self, bank_id: str, memory_id: str) -> dict[str, Any]:
        """DELETE /v1/default/banks/{bank_id}/memories/{memory_id}/observations

        Clears all observations derived from this memory. The memory itself is preserved.
        """
        return self._request(
            "DELETE",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/{urllib.parse.quote(memory_id, safe='')}/observations",
        )

    def list_banks(self) -> list[str]:
        """GET /v1/default/banks — return bank ids only (the rest of the payload is unused).

        Used by the main entry point to verify a resolved bank id actually exists on the
        server before issuing a reflect call. The official plugin's config may name a bank that
        has not been created yet; we silently skip in that case.
        """
        resp = self._request("GET", "/v1/default/banks")
        banks = resp.get("banks", []) if isinstance(resp, dict) else []
        ids: list[str] = []
        for item in banks:
            if isinstance(item, dict) and isinstance(item.get("bank_id"), str):
                ids.append(item["bank_id"])
        return ids

    def list_memory_units(
        self,
        bank_id: str,
        document_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """GET /v1/default/banks/{bank_id}/memories/list?document_id=...

        Used by the webhook handler to pull the freshly retained memory_units for the
        just-completed document so we can run reconcile per-unit. Pagination is naive:
        we keep calling until the page comes back smaller than ``limit`` or we hit a
        hard cap to avoid runaway loops on a misconfigured server.

        Response shape (per Hindsight v0.8.x):

            {"items": [{...memory_unit dicts...}], "total": N, "limit": L, "offset": O}

        Each unit carries ``id`` (UUID), ``text`` (the extracted fact body), ``fact_type``
        (world / experience / observation), and ``document_id``.
        """
        path = f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/list"
        all_units: list[dict[str, Any]] = []
        for _ in range(50):  # 50 * 100 = 5000 units ceiling per document
            resp = self._request(
                "GET",
                path,
                query={"document_id": document_id, "limit": limit, "offset": offset},
            )
            # Hindsight returns the page under "items", not "memory_units".
            page = resp.get("items", []) if isinstance(resp, dict) else []
            if not isinstance(page, list):
                page = []
            all_units.extend(u for u in page if isinstance(u, dict))
            if len(page) < limit:
                break
            offset += limit
        return all_units