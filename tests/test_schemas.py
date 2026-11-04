import math
import unittest

from pydantic import ValidationError

from ai_field_assessment.task1_mcp_server.schemas import CustomerInput, RefundInput


class SchemaTests(unittest.TestCase):
    def test_customer_identifier(self):
        self.assertEqual(CustomerInput(customer_id="CUST-12345").customer_id, "CUST-12345")
        for value in [None, 12345, "cust-12345", "CUST-123456", "CUST-12345\n", "CUST-１２３４５"]:
            with self.assertRaises(ValidationError):
                CustomerInput(customer_id=value)
        with self.assertRaises(ValidationError):
            CustomerInput(customer_id="CUST-12345", unexpected=True)

    def test_refund_numbers_and_reason(self):
        for amount in [0, -1, True, "75.50", math.inf, math.nan]:
            with self.assertRaises(ValidationError):
                RefundInput(customer_id="CUST-12345", amount=amount, reason="Damaged item")
        for amount in [1, 75.50]:
            self.assertGreater(RefundInput(
                customer_id="CUST-12345", amount=amount, reason="Damaged item"
            ).amount, 0)
        with self.assertRaises(ValidationError):
            RefundInput(customer_id="CUST-12345", amount=1, reason="short")
