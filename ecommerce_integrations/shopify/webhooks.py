from dataclasses import dataclass

import frappe
from frappe import _

from ecommerce_integrations.shopify.constants import GRAPHQL_WEBHOOK_TOPICS, REST_WEBHOOK_TOPICS, WEBHOOK_EVENTS
from ecommerce_integrations.shopify.graphql import ShopifyGraphQLClient


LIST_WEBHOOKS_QUERY = """
query ShopifyWebhookSubscriptions($first: Int!) {
	webhookSubscriptions(first: $first) {
		edges {
			node {
				id
				topic
				uri
			}
		}
	}
}
"""

CREATE_WEBHOOK_MUTATION = """
mutation ShopifyCreateWebhook($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
	webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
		webhookSubscription {
			id
			topic
			uri
		}
		userErrors {
			field
			message
		}
	}
}
"""

DELETE_WEBHOOK_MUTATION = """
mutation ShopifyDeleteWebhook($id: ID!) {
	webhookSubscriptionDelete(id: $id) {
		deletedWebhookSubscriptionId
		userErrors {
			field
			message
		}
	}
}
"""


@dataclass
class ShopifyWebhookSubscription:
	id: str
	topic: str
	uri: str

	@property
	def rest_topic(self) -> str:
		return REST_WEBHOOK_TOPICS.get(self.topic, self.topic)


def get_current_domain_name() -> str:
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url

	if getattr(frappe, "request", None):
		return frappe.request.host

	return frappe.utils.get_url().removeprefix("https://").removeprefix("http://")


def get_callback_url() -> str:
	return (
		f"https://{get_current_domain_name()}"
		"/api/method/ecommerce_integrations.shopify.connection.store_request_data"
	)


def list_webhooks(setting) -> list[ShopifyWebhookSubscription]:
	client = ShopifyGraphQLClient(setting)
	data = client.execute(LIST_WEBHOOKS_QUERY, {"first": 50})
	edges = (data.get("webhookSubscriptions") or {}).get("edges") or []

	return [
		ShopifyWebhookSubscription(
			id=edge["node"]["id"], topic=edge["node"]["topic"], uri=edge["node"]["uri"]
		)
		for edge in edges
		if edge.get("node")
	]


def register_webhooks(setting) -> list[ShopifyWebhookSubscription]:
	return sync_webhooks(setting, remove_stale=True)


def unregister_webhooks(setting) -> None:
	callback_url = get_callback_url()
	client = ShopifyGraphQLClient(setting)
	for webhook in list_webhooks(setting):
		if webhook.uri == callback_url:
			_delete_webhook(client, webhook.id)


def sync_webhooks(setting, remove_stale: bool = True) -> list[ShopifyWebhookSubscription]:
	client = ShopifyGraphQLClient(setting)
	callback_url = get_callback_url()
	remote_webhooks = [webhook for webhook in list_webhooks(setting) if webhook.uri == callback_url]
	indexed_by_topic: dict[str, list[ShopifyWebhookSubscription]] = {}
	for webhook in remote_webhooks:
		indexed_by_topic.setdefault(webhook.rest_topic, []).append(webhook)

	active_webhooks: list[ShopifyWebhookSubscription] = []
	for topic in WEBHOOK_EVENTS:
		matches = indexed_by_topic.get(topic, [])
		if matches:
			active_webhooks.append(matches[0])
			for duplicate in matches[1:]:
				_delete_webhook(client, duplicate.id)
			continue

		active_webhooks.append(_create_webhook(client, topic, callback_url))

	if remove_stale:
		required_topics = set(WEBHOOK_EVENTS)
		for webhook in remote_webhooks:
			if webhook.rest_topic not in required_topics:
				_delete_webhook(client, webhook.id)

	return active_webhooks


def sync_setting_webhooks(setting, remove_stale: bool = True) -> list[ShopifyWebhookSubscription]:
	active_webhooks = sync_webhooks(setting, remove_stale=remove_stale)
	setting.webhooks = []
	for webhook in active_webhooks:
		setting.append("webhooks", {"webhook_id": webhook.id, "method": webhook.rest_topic})

	return active_webhooks


def clear_setting_webhooks(setting) -> None:
	unregister_webhooks(setting)
	setting.webhooks = []


def _create_webhook(
	client: ShopifyGraphQLClient, rest_topic: str, callback_url: str
) -> ShopifyWebhookSubscription:
	data = client.execute(
		CREATE_WEBHOOK_MUTATION,
		{
			"topic": GRAPHQL_WEBHOOK_TOPICS[rest_topic],
			"webhookSubscription": {"uri": callback_url, "format": "JSON"},
		},
	)
	result = data.get("webhookSubscriptionCreate") or {}
	user_errors = result.get("userErrors") or []
	if user_errors:
		frappe.throw("\n".join(error.get("message") or _("Webhook creation failed") for error in user_errors))

	webhook = result.get("webhookSubscription") or {}
	return ShopifyWebhookSubscription(
		id=webhook.get("id"),
		topic=webhook.get("topic"),
		uri=webhook.get("uri"),
	)


def _delete_webhook(client: ShopifyGraphQLClient, webhook_id: str) -> None:
	data = client.execute(DELETE_WEBHOOK_MUTATION, {"id": webhook_id})
	result = data.get("webhookSubscriptionDelete") or {}
	user_errors = result.get("userErrors") or []
	if user_errors:
		frappe.throw("\n".join(error.get("message") or _("Webhook deletion failed") for error in user_errors))