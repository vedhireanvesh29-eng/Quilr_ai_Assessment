from decimal import Decimal
from uuid import uuid4

CUSTOMERS = {
    "CUST-12345": {"customer_id": "CUST-12345", "name": "Demo Customer", "status": "active"}
}


def get_customer(customer_id: str) -> dict:
    if customer_id not in CUSTOMERS:
        raise ValueError("Customer not found")
    return dict(CUSTOMERS[customer_id])


def refund(customer_id: str, amount: float, reason: str) -> dict:
    get_customer(customer_id)
    # Decimal is used for money; string output preserves decimal precision.
    value = Decimal(str(amount))
    return {
        "refund_id": f"mock-{uuid4().hex}",
        "customer_id": customer_id,
        "amount": str(value),
        "reason": reason,
        "status": "simulated",
    }
