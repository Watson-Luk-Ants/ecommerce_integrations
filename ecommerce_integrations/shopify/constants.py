# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE


MODULE_NAME = "shopify"
SETTING_DOCTYPE = "Shopify Setting"

DEFAULT_API_VERSION = "2026-01"
SUPPORTED_API_VERSIONS = "2026-01\n2025-10\n2025-07\n2025-04\n2025-01"
API_VERSION = DEFAULT_API_VERSION

# OAuth grant used to obtain the Shopify Admin API access token.
CLIENT_CREDENTIALS_GRANT_TYPE = "client_credentials"

WEBHOOK_EVENTS = [
	"orders/create",
	"orders/paid",
	"orders/fulfilled",
	"orders/cancelled",
	"orders/partially_fulfilled",
]

EVENT_MAPPER = {
	"orders/create": "ecommerce_integrations.shopify.order.sync_sales_order",
	"orders/paid": "ecommerce_integrations.shopify.invoice.prepare_sales_invoice",
	"orders/fulfilled": "ecommerce_integrations.shopify.fulfillment.prepare_delivery_note",
	"orders/cancelled": "ecommerce_integrations.shopify.order.cancel_order",
	"orders/partially_fulfilled": "ecommerce_integrations.shopify.fulfillment.prepare_delivery_note",
}

GRAPHQL_WEBHOOK_TOPICS = {
	"orders/create": "ORDERS_CREATE",
	"orders/paid": "ORDERS_PAID",
	"orders/fulfilled": "ORDERS_FULFILLED",
	"orders/cancelled": "ORDERS_CANCELLED",
	"orders/partially_fulfilled": "ORDERS_PARTIALLY_FULFILLED",
}

REST_WEBHOOK_TOPICS = {topic: event for event, topic in GRAPHQL_WEBHOOK_TOPICS.items()}

SHOPIFY_VARIANTS_ATTR_LIST = ["option1", "option2", "option3"]

# custom fields

CUSTOMER_ID_FIELD = "shopify_customer_id"
ORDER_ID_FIELD = "shopify_order_id"
ORDER_NUMBER_FIELD = "shopify_order_number"
ORDER_STATUS_FIELD = "shopify_order_status"
FULLFILLMENT_ID_FIELD = "shopify_fulfillment_id"
SUPPLIER_ID_FIELD = "shopify_supplier_id"
ADDRESS_ID_FIELD = "shopify_address_id"
ORDER_ITEM_DISCOUNT_FIELD = "shopify_item_discount"
ITEM_SELLING_RATE_FIELD = "shopify_selling_rate"

# ERPNext already defines the default UOMs from Shopify but names are different
WEIGHT_TO_ERPNEXT_UOM_MAP = {"kg": "Kg", "g": "Gram", "oz": "Ounce", "lb": "Pound"}
