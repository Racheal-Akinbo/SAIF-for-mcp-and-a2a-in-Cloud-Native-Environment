"""Shared mock backend used by both the MCP and A2A implementations.

Keeping the underlying data and business logic identical is what makes the
MCP-vs-A2A comparison fair: the only thing that differs between the two
apps is the *protocol* used to expose and invoke the same capability.
"""

ORDERS = {
    "ORD-1001": {"status": "shipped", "eta_days": 2, "carrier": "UPS"},
    "ORD-1002": {"status": "processing", "eta_days": 5, "carrier": None},
    "ORD-1003": {"status": "delivered", "eta_days": 0, "carrier": "FedEx"},
}

VALID_TOKEN = "demo-shared-secret-token"


def lookup_order(order_id: str) -> dict:
    if order_id not in ORDERS:
        raise KeyError(f"No such order: {order_id}")
    return {"order_id": order_id, **ORDERS[order_id]}
