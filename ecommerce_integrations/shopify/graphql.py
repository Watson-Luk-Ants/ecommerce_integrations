from typing import Any

import requests

import frappe
from frappe import _

from ecommerce_integrations.shopify.auth import (
	can_refresh_access_token,
	ensure_access_token,
	get_api_version,
	normalize_shop_url,
	refresh_access_token,
	validate_client_credentials,
)


JsonDict = dict[str, Any]
GRAPHQL_TIMEOUT = 30


class ShopifyGraphQLClient:
	def __init__(self, setting):
		self.setting = setting
		self.shop_url = normalize_shop_url(setting.shopify_url)
		self.api_version = get_api_version(setting)
		self.access_token = ensure_access_token(setting)

		if not self.shop_url or not self.access_token:
			frappe.throw(_("Shopify connection requires a shop URL and access token."))

	@property
	def endpoint(self) -> str:
		return f"https://{self.shop_url}/admin/api/{self.api_version}/graphql.json"

	def execute(self, query: str, variables: JsonDict | None = None) -> JsonDict:
		response = self._post(query, variables)

		# The token may have been revoked or expired earlier than its stored
		# expiry. Refresh once via the client credentials grant and retry.
		if response.status_code == 401 and can_refresh_access_token(self.setting):
			self.access_token = refresh_access_token(self.setting)
			response = self._post(query, variables)

		response.raise_for_status()

		payload = response.json()
		if payload.get("errors"):
			frappe.throw(_format_graphql_error(payload["errors"]))

		return payload.get("data") or {}

	def _post(self, query: str, variables: JsonDict | None = None):
		return requests.post(
			self.endpoint,
			json={"query": query, "variables": variables or {}},
			headers={
				"Content-Type": "application/json",
				"Accept": "application/json",
				"X-Shopify-Access-Token": self.access_token,
			},
			timeout=GRAPHQL_TIMEOUT,
		)


def test_connection(setting) -> JsonDict:
	if not normalize_shop_url(getattr(setting, "shopify_url", None)):
		frappe.throw(_("Shop URL is required to test the Shopify connection."))
	validate_client_credentials(setting)

	# Acquires a token via the client credentials grant if one isn't stored yet, so
	# this verifies credentials + app installation + API reachability end to end.
	client = ShopifyGraphQLClient(setting)
	data = client.execute(
		"""
		query ShopifyConnectionTest {
		  shop {
		    id
		    name
		    myshopifyDomain
		  }
		}
		"""
	)

	shop = data.get("shop") or {}
	if not shop:
		frappe.throw(_("Shopify connection test returned an empty response."))

	shop["status"] = "connected"
	shop["message"] = _("Connected to Shopify shop {0}.").format(shop.get("name"))
	return shop


def _format_graphql_error(errors: list[JsonDict]) -> str:
	return "\n".join(error.get("message") or _("Unknown Shopify GraphQL error") for error in errors)
