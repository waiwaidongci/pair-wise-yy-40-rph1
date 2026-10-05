from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def _item_id(self, path: str) -> int:
            return int(path.split("/")[3])

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    _, role = self._identity()
                    status = query.get("status", [None])[0]
                    self._json(200, {"items": service.list_items(role, status)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._item_id(path)
                    _, role = self._identity()
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/") and path.endswith("/components"):
                    item_id = self._item_id(path)
                    _, role = self._identity()
                    version_status = query.get("status", [None])[0]
                    self._json(200, {"components": service.list_components(
                        item_id, role, version_status)})
                elif path.startswith("/api/items/") and path.endswith("/schemes"):
                    item_id = self._item_id(path)
                    _, role = self._identity()
                    self._json(200, {"schemes": service.list_schemes(item_id, role)})
                elif path.startswith("/api/items/") and path.endswith("/conclusions"):
                    item_id = self._item_id(path)
                    _, role = self._identity()
                    self._json(200, {"conclusions": service.list_conclusions(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    _, role = self._identity()
                    self._json(200, service.get_item(item_id, role))
                elif path.startswith("/api/batches/"):
                    batch_no = path.rsplit("/", 1)[-1]
                    _, role = self._identity()
                    self._json(200, service.get_batch(batch_no, role))
                elif path == "/api/audit":
                    _, role = self._identity()
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/batches":
                    self._json(201, service.submit_batch(body, actor, role))
                elif path.startswith("/api/component-versions/") and path.endswith("/promote"):
                    version_id = int(path.split("/")[3])
                    self._json(200, service.promote_component_version(version_id, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._item_id(path)
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = self._item_id(path)
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/review-decision"):
                    item_id = self._item_id(path)
                    self._json(200, service.decide_review(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/backfill"):
                    item_id = self._item_id(path)
                    self._json(200, service.backfill_item(item_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
