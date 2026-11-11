"""Steady traffic: mostly product reads, some order listings, receipts and checkouts."""

import random

from locust import HttpUser, constant_throughput, task


class Shopper(HttpUser):
    wait_time = constant_throughput(1)

    @task(6)
    def product(self):
        sku = random.choice(["SKU-HOT-1"] + [f"SKU-{random.randint(1, 200)}" for _ in range(3)])
        self.client.get(f"/products/{sku}", name="/products/{sku}")

    @task(2)
    def orders(self):
        self.client.get(f"/orders?customer_id=cust-{random.randint(1, 5000)}", name="/orders")

    @task(1)
    def receipt(self):
        self.client.get(f"/receipts/{random.randint(1, 200000)}", name="/receipts/{id}")

    @task(3)
    def checkout(self):
        sku = random.choice(["SKU-HOT-1", f"SKU-{random.randint(1, 200)}"])
        self.client.post("/checkout", name="/checkout", json={
            "customer_id": f"cust-{random.randint(1, 5000)}",
            "items": [{"sku": sku, "qty": random.randint(1, 2), "price_cents": 1500}],
        })
