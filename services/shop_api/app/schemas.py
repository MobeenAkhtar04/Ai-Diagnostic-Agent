from pydantic import BaseModel


class Item(BaseModel):
    sku: str
    qty: int = 1
    price_cents: int = 1000


class CheckoutRequest(BaseModel):
    customer_id: str
    items: list[Item]
    shipping_address: dict | None = None
    coupon_code: str | None = None
