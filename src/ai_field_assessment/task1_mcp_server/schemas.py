from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

CustomerID = Annotated[str, Field(strict=True, pattern=r"^CUST-[0-9]{5}$")]
Amount = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Reason = Annotated[str, Field(strict=True, min_length=10)]


class CustomerInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: CustomerID


class RefundInput(CustomerInput):
    amount: Amount
    reason: Reason
