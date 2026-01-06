from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

OPENAPI_TAGS = [
    {"name": "Health", "description": "Basic service health checks."},
    {"name": "Calculator", "description": "Perform arithmetic operations and persist calculation history."},
]


def _utc_now_iso() -> str:
    """Return current UTC time as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat()


class CalculationCreateRequest(BaseModel):
    """Request payload to create a calculation."""

    a: float = Field(..., description="First operand.")
    b: float = Field(..., description="Second operand.")
    op: Literal["+", "-", "*", "/"] = Field(..., description="Arithmetic operator.")

    @model_validator(mode="after")
    def _validate(self) -> "CalculationCreateRequest":
        if self.op == "/" and self.b == 0:
            raise ValueError("Division by zero is not allowed.")
        return self


class CalculationResponse(BaseModel):
    """Response payload representing a calculation result and stored history item."""

    id: int = Field(..., description="History record ID.")
    a: float = Field(..., description="First operand.")
    b: float = Field(..., description="Second operand.")
    op: Literal["+", "-", "*", "/"] = Field(..., description="Arithmetic operator.")
    result: float = Field(..., description="Computed result.")
    created_at: str = Field(..., description="UTC ISO8601 timestamp when this record was stored.")


class HistoryListResponse(BaseModel):
    """Response payload for history list."""

    items: list[CalculationResponse] = Field(..., description="History items, newest first.")


def _get_db_path() -> str:
    """
    Resolve SQLite DB path.

    Note: No .env variables are configured for this project per task context,
    so we default to a local file within the container. This will work in-container
    and persists for the container's lifetime.
    """
    return os.getenv("CALCULATOR_DB_PATH", os.path.join(os.getcwd(), "calculator.sqlite3"))


def _connect(db_path: str) -> sqlite3.Connection:
    """Create a sqlite3 connection with sensible defaults."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Initialize schema if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calculation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            a REAL NOT NULL,
            b REAL NOT NULL,
            op TEXT NOT NULL,
            result REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _compute(a: float, b: float, op: str) -> float:
    """Compute the arithmetic result."""
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        # division-by-zero validated earlier, but keep defense-in-depth
        if b == 0:
            raise ZeroDivisionError("Division by zero")
        return a / b
    raise ValueError(f"Unsupported operator: {op}")


app = FastAPI(
    title="Calculator API",
    description=(
        "REST API for a fullstack calculator. Supports basic arithmetic (+, -, *, /) "
        "and stores/retrieves calculation history."
    ),
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
)

# CORS: backend on 3001, frontend on 3000 (explicitly per requirement)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    """Initialize SQLite database and schema on startup."""
    db_path = _get_db_path()
    conn = _connect(db_path)
    try:
        _init_db(conn)
    finally:
        conn.close()


@app.get("/", tags=["Health"], summary="Health check")
# PUBLIC_INTERFACE
def health_check() -> dict:
    """Health check endpoint. Returns a simple JSON payload."""
    return {"message": "Healthy"}


@app.post(
    "/api/calculate",
    tags=["Calculator"],
    response_model=CalculationResponse,
    summary="Perform an arithmetic operation and store the result in history",
)
# PUBLIC_INTERFACE
def calculate(payload: CalculationCreateRequest) -> CalculationResponse:
    """
    Perform the requested arithmetic operation, validate inputs, store the result, and return it.

    Parameters:
      - payload: operands `a`, `b` and operator `op` in {+,-,*,/}.

    Returns:
      - A stored calculation record including `id` and `created_at`.

    Raises:
      - 422 validation error for invalid payload (including divide-by-zero).
      - 400 for unsupported operator or other compute errors.
    """
    try:
        result = _compute(payload.a, payload.b, payload.op)
    except ZeroDivisionError:
        raise HTTPException(status_code=400, detail="Division by zero is not allowed.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    created_at = _utc_now_iso()
    db_path = _get_db_path()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO calculation_history (a, b, op, result, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payload.a, payload.b, payload.op, float(result), created_at),
        )
        conn.commit()
        new_id = int(cur.lastrowid)
    finally:
        conn.close()

    return CalculationResponse(
        id=new_id,
        a=payload.a,
        b=payload.b,
        op=payload.op,
        result=float(result),
        created_at=created_at,
    )


@app.get(
    "/api/history",
    tags=["Calculator"],
    response_model=HistoryListResponse,
    summary="Get calculation history",
)
# PUBLIC_INTERFACE
def get_history(limit: int = 25) -> HistoryListResponse:
    """
    Retrieve calculation history, newest first.

    Parameters:
      - limit: max number of history items to return (default 25; capped at 200).

    Returns:
      - A list of stored calculation records.
    """
    limit = max(1, min(int(limit), 200))
    db_path = _get_db_path()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, a, b, op, result, created_at
            FROM calculation_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    items = [
        CalculationResponse(
            id=int(r["id"]),
            a=float(r["a"]),
            b=float(r["b"]),
            op=str(r["op"]),  # type: ignore[arg-type]
            result=float(r["result"]),
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]
    return HistoryListResponse(items=items)


@app.delete(
    "/api/history",
    tags=["Calculator"],
    summary="Clear calculation history",
)
# PUBLIC_INTERFACE
def clear_history() -> dict:
    """
    Delete all history records.

    Returns:
      - JSON with deleted row count.
    """
    db_path = _get_db_path()
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM calculation_history")
        conn.commit()
        deleted = cur.rowcount if cur.rowcount is not None else 0
    finally:
        conn.close()

    return {"deleted": deleted}
