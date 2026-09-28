"""Small typed Paperclip HTTP boundary used by Nerve supervision."""
from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.request
from typing import Any, Protocol


@dataclass(frozen=True)
class PaperclipIssue:
    id: str
    title: str
    description: str
    status: str
    project_id: str | None = None
    assignee_agent_id: str | None = None
    checkout_run_id: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        ...


class PaperclipError(RuntimeError):
    pass


class PaperclipTransportError(PaperclipError):
    pass


class UrlLibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        payload = None
        request_headers = dict(headers or {})
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            url, data=payload, headers=request_headers, method=str(method).upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=float(timeout)) as response:
                raw = response.read()
                body = json.loads(raw.decode("utf-8")) if raw else None
                return HttpResponse(int(response.status), body)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                body = raw.decode("utf-8", errors="replace")
            return HttpResponse(int(exc.code), body)


class PaperclipClient:
    """Minimal issue API client.

    Reads may retry transient transport failures. Mutations are attempted once:
    callers reconcile the issue state before retrying so a timed-out PATCH cannot
    create duplicate verification comments.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        run_id: str,
        transport: HttpTransport | None = None,
        read_attempts: int = 3,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "").strip()
        self.run_id = str(run_id or "").strip()
        self.transport = transport or UrlLibTransport()
        self.read_attempts = max(1, min(int(read_attempts), 3))
        self.timeout = max(0.1, float(timeout))
        if not self.base_url:
            raise ValueError("Paperclip API base URL is required")
        if not self.token:
            raise ValueError("Paperclip API token is required")
        if not self.run_id:
            raise ValueError("Paperclip run id is required")

    def _headers(self, *, mutation: bool) -> dict[str, str]:
        out = {"Authorization": f"Bearer {self.token}"}
        if mutation:
            out["X-Paperclip-Run-Id"] = self.run_id
        return out

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        retry_read: bool = False,
    ) -> Any:
        attempts = self.read_attempts if retry_read else 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                response = self.transport.request(
                    method,
                    self.base_url + path,
                    json_body=payload,
                    headers=self._headers(mutation=str(method).upper() != "GET"),
                    timeout=self.timeout,
                )
            except Exception as exc:
                last = exc
                continue
            if 200 <= int(response.status) < 300:
                return response.body
            message = response.body
            raise PaperclipTransportError(
                f"Paperclip HTTP {response.status}: {message}"
            )
        raise PaperclipTransportError(str(last or "Paperclip request failed")) from last

    def get_issue(self, issue_id: str) -> PaperclipIssue:
        issue_id = str(issue_id or "").strip()
        if not issue_id:
            raise ValueError("issue_id is required")
        data = self._request("GET", f"/api/issues/{issue_id}", retry_read=True)
        if not isinstance(data, dict):
            raise PaperclipError("invalid Paperclip issue response")
        try:
            issue = PaperclipIssue(
                id=str(data["id"]).strip(),
                title=str(data["title"]).strip(),
                description=str(data.get("description") or ""),
                status=str(data["status"]).strip(),
                project_id=(
                    str(data["projectId"]).strip()
                    if data.get("projectId") is not None else None
                ),
                assignee_agent_id=(
                    str(data["assigneeAgentId"]).strip()
                    if data.get("assigneeAgentId") is not None else None
                ),
                checkout_run_id=(
                    str(data["checkoutRunId"]).strip()
                    if data.get("checkoutRunId") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperclipError("invalid Paperclip issue schema") from exc
        if not issue.id or not issue.title or not issue.status:
            raise PaperclipError("invalid Paperclip issue fields")
        return issue

    def request_review(self, issue_id: str, *, comment: str) -> PaperclipIssue:
        """Atomically move an issue to in_review and attach the verification comment."""
        data = self._request(
            "PATCH",
            f"/api/issues/{issue_id}",
            payload={"status": "in_review", "comment": str(comment)},
        )
        if not isinstance(data, dict):
            # Paperclip normally returns the updated issue. Re-read if a compatible
            # deployment returns an empty success body.
            return self.get_issue(issue_id)
        return self._parse_issue_like(data)

    def revert_to_in_progress(self, issue_id: str, *, comment: str) -> PaperclipIssue:
        """Controller-only: pull an unverified issue out of review."""
        data = self._request(
            "PATCH",
            f"/api/issues/{issue_id}",
            payload={"status": "in_progress", "comment": str(comment)},
        )
        if not isinstance(data, dict):
            return self.get_issue(issue_id)
        return self._parse_issue_like(data)

    def create_confirmation_interaction(
        self,
        issue_id: str,
        *,
        prompt: str,
        details: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Create the review-path confirmation interaction (idempotent per run)."""
        data = self._request(
            "POST",
            f"/api/issues/{issue_id}/interactions",
            payload={
                "kind": "request_confirmation",
                "idempotencyKey": f"nerve-handoff:{run_id}",
                "continuationPolicy": "none",
                "payload": {
                    "version": 1,
                    "prompt": str(prompt)[:1000],
                    "detailsMarkdown": str(details)[:20000],
                    "allowDeclineReason": True,
                },
            },
        )
        return data if isinstance(data, dict) else None

    def _parse_issue_like(self, data: dict[str, Any]) -> PaperclipIssue:
        issue_data = data.get("issue") if isinstance(data.get("issue"), dict) else data
        try:
            return PaperclipIssue(
                id=str(issue_data["id"]).strip(),
                title=str(issue_data["title"]).strip(),
                description=str(issue_data.get("description") or ""),
                status=str(issue_data["status"]).strip(),
                project_id=str(issue_data["projectId"]).strip() if issue_data.get("projectId") is not None else None,
                assignee_agent_id=str(issue_data["assigneeAgentId"]).strip() if issue_data.get("assigneeAgentId") is not None else None,
                checkout_run_id=str(issue_data["checkoutRunId"]).strip() if issue_data.get("checkoutRunId") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperclipError("invalid Paperclip issue schema") from exc
