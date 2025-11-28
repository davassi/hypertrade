"""SQLite database layer for persisting order operations and failures."""

import os
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

log = logging.getLogger("uvicorn.error")


class OrderDatabase:
    """SQLite database for storing order operations and failures."""

    def __init__(self, db_path: str = "./hypertrade.db"):
        """Initialize database connection and create tables if needed.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_db_exists()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db_exists(self) -> None:
        """Create database tables if they don't exist."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Orders table: tracks all executed orders (successful and failed)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                signal TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                leverage INTEGER,
                subaccount TEXT,
                status TEXT NOT NULL,
                order_id TEXT,
                avg_price REAL,
                total_size REAL,
                response_json TEXT,
                execution_ms REAL
            )
        """)

        # Failures table: detailed failure information
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                request_id TEXT,
                timestamp TEXT NOT NULL,
                error_type TEXT NOT NULL,
                error_message TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """)

        # Create indices for faster queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_request_id ON orders(request_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failures_order_id ON failures(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failures_timestamp ON failures(timestamp)")

        conn.commit()
        conn.close()

        # Restrict database file permissions to owner only (rw-------)
        try:
            os.chmod(self.db_path, 0o600)
            log.debug("Database file permissions restricted to owner only")
        except OSError as e:
            log.warning("Could not restrict database file permissions: %s", e)

        log.info("Database initialized: %s", self.db_path)

    def log_order(
        self,
        request_id: str,
        symbol: str,
        side: str,
        signal: str,
        quantity: float,
        price: float,
        status: str,
        leverage: Optional[int] = None,
        subaccount: Optional[str] = None,
        order_id: Optional[str] = None,
        avg_price: Optional[float] = None,
        total_size: Optional[float] = None,
        response_json: Optional[str] = None,
        execution_ms: Optional[float] = None,
    ) -> int:
        """Log an order execution to the database.

        Args:
            request_id: Unique request identifier
            symbol: Trading symbol (e.g., 'ETHUSDT')
            side: Order side (BUY/SELL)
            signal: Signal type (OPEN_LONG, CLOSE_LONG, etc.)
            quantity: Order quantity
            price: Order price
            status: Status (PLACED, FILLED, FAILED, REJECTED)
            leverage: Order leverage multiplier
            subaccount: Subaccount address if trading on subaccount
            order_id: Exchange order ID
            avg_price: Average execution price
            total_size: Total size executed
            response_json: Full response JSON from exchange
            execution_ms: Execution time in milliseconds

        Returns:
            Order ID in database
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO orders (
                    request_id, timestamp, symbol, side, signal, quantity, price,
                    leverage, subaccount, status, order_id, avg_price, total_size,
                    response_json, execution_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request_id,
                datetime.now(timezone.utc).isoformat(),
                symbol,
                side,
                signal,
                quantity,
                price,
                leverage,
                subaccount,
                status,
                order_id,
                avg_price,
                total_size,
                response_json,
                execution_ms,
            ))
            conn.commit()
            order_pk = cursor.lastrowid
            log.debug(
                "Order logged: id=%d request_id=%s symbol=%s side=%s status=%s",
                order_pk, request_id, symbol, side, status
            )
            return order_pk
        except sqlite3.IntegrityError:
            log.warning("Duplicate request_id: %s", request_id)
            raise
        finally:
            conn.close()

    def log_failure(
        self,
        request_id: str,
        error_type: str,
        error_message: str,
        attempt: int = 1,
        retry_count: int = 0,
        order_id: Optional[int] = None,
    ) -> int:
        """Log an order failure or error.

        Args:
            request_id: Unique request identifier
            error_type: Error class name (HyperliquidValidationError, etc.)
            error_message: Human-readable error message
            attempt: Current attempt number
            retry_count: Number of retries performed
            order_id: Foreign key to orders table (if available)

        Returns:
            Failure log ID in database
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO failures (
                    order_id, request_id, timestamp, error_type, error_message,
                    attempt, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                request_id,
                datetime.now(timezone.utc).isoformat(),
                error_type,
                error_message,
                attempt,
                retry_count,
            ))
            conn.commit()
            failure_id = cursor.lastrowid
            log.debug(
                "Failure logged: id=%d request_id=%s error_type=%s attempt=%d",
                failure_id, request_id, error_type, attempt
            )
            return failure_id
        finally:
            conn.close()

    def get_orders(
        self,
        limit: int = 100,
        offset: int = 0,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        side: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query orders from database.

        Args:
            limit: Maximum number of records to return
            offset: Number of records to skip
            symbol: Filter by symbol
            status: Filter by status
            side: Filter by side (BUY/SELL)

        Returns:
            List of order dictionaries
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM orders WHERE 1=1"
        params: List[Any] = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if status:
            query += " AND status = ?"
            params.append(status)
        if side:
            query += " AND side = ?"
            params.append(side)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    def get_failures(
        self,
        limit: int = 100,
        offset: int = 0,
        error_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query failures from database.

        Args:
            limit: Maximum number of records to return
            offset: Number of records to skip
            error_type: Filter by error type

        Returns:
            List of failure dictionaries
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM failures WHERE 1=1"
        params: List[Any] = []

        if error_type:
            query += " AND error_type = ?"
            params.append(error_type)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    def get_order_by_request_id(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get a single order by request ID.

        Args:
            request_id: Request identifier

        Returns:
            Order dictionary or None if not found
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM orders WHERE request_id = ?", (request_id,))
        row = cursor.fetchone()
        conn.close()

        return dict(row) if row else None

    def get_failures_by_order_id(self, order_id: int) -> List[Dict[str, Any]]:
        """Get all failures for a specific order.

        Args:
            order_id: Order ID in database

        Returns:
            List of failure dictionaries
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM failures WHERE order_id = ? ORDER BY timestamp", (order_id,))
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    def get_statistics(self) -> Dict[str, Any]:
        """Get summary statistics about orders and failures.

        Returns:
            Dictionary with stats
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total_orders FROM orders")
        total_orders = cursor.fetchone()["total_orders"]

        cursor.execute("SELECT COUNT(*) as failed_orders FROM orders WHERE status IN ('FAILED', 'REJECTED')")
        failed_orders = cursor.fetchone()["failed_orders"]

        cursor.execute("SELECT COUNT(*) as total_failures FROM failures")
        total_failures = cursor.fetchone()["total_failures"]

        cursor.execute("""
            SELECT symbol, COUNT(*) as count FROM orders
            GROUP BY symbol ORDER BY count DESC LIMIT 5
        """)
        top_symbols = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT error_type, COUNT(*) as count FROM failures
            GROUP BY error_type ORDER BY count DESC LIMIT 5
        """)
        top_errors = [dict(row) for row in cursor.fetchall()]

        conn.close()

        return {
            "total_orders": total_orders,
            "failed_orders": failed_orders,
            "success_rate": (total_orders - failed_orders) / total_orders * 100 if total_orders > 0 else 0,
            "total_failures": total_failures,
            "top_symbols": top_symbols,
            "top_errors": top_errors,
        }
