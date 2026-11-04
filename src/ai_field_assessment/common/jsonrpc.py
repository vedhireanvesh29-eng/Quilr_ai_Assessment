from typing import Any


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def validate_request(body: Any) -> dict:
    """This demonstration HTTP proxy accepts individual requests, not batches/notifications."""
    if not isinstance(body, dict):
        raise RPCError(-32600, "Invalid Request")
    request_id = body.get("id")
    if (
        body.get("jsonrpc") != "2.0"
        or "id" not in body
        or isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, type(None)))
        or not isinstance(body.get("method"), str)
    ):
        raise RPCError(-32600, "Invalid Request")
    if "params" in body and not isinstance(body["params"], (dict, list)):
        raise RPCError(-32602, "Invalid params")
    return body
