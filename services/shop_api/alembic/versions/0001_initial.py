"""initial schema

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("products", sa.Column("sku", sa.String, primary_key=True), sa.Column("name", sa.String),
                    sa.Column("price_cents", sa.Integer))
    op.create_table("inventory", sa.Column("sku", sa.String, primary_key=True), sa.Column("qty", sa.Integer),
                    sa.CheckConstraint("qty >= 0", name="inventory_qty_nonneg"))
    op.create_table("orders", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("customer_id", sa.String), sa.Column("total_cents", sa.Integer),
                    sa.Column("status", sa.String, server_default="paid"),
                    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()))
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_table("order_items", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id")),
                    sa.Column("product_id", sa.Integer, server_default="0"), sa.Column("sku", sa.String),
                    sa.Column("qty", sa.Integer), sa.Column("price_cents", sa.Integer))
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_table("fulfilment_jobs", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("order_id", sa.Integer), sa.Column("status", sa.String, server_default="queued"),
                    sa.Column("attempts", sa.Integer, server_default="0"),
                    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()))
    op.create_table("payments", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("order_id", sa.Integer), sa.Column("amount_cents", sa.Integer),
                    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()))
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_table("deploys", sa.Column("id", sa.Integer, primary_key=True), sa.Column("kind", sa.String),
                    sa.Column("service", sa.String), sa.Column("git_sha", sa.String),
                    sa.Column("previous_sha", sa.String), sa.Column("detail", sa.String),
                    sa.Column("by", sa.String, server_default="ci"),
                    sa.Column("at", sa.DateTime, server_default=sa.func.now()))


def downgrade():
    for t in ("deploys", "payments", "fulfilment_jobs", "order_items", "orders", "inventory", "products"):
        op.drop_table(t)
