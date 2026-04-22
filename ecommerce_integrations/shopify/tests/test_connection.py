# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import base64
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import set_encrypted_password

from ecommerce_integrations.shopify import auth, connection
from ecommerce_integrations.shopify.webhooks import ShopifyWebhookSubscription

from .utils import TestCase


class TestShopifyConnection(TestCase):
	def store_secret(self, fieldname, value):
		"""Persist a Password-field value (access_token / client_secret) to the auth store."""
		set_encrypted_password(self.setting.doctype, self.setting.name, value, fieldname)

	def test_get_access_token_returns_stored_token(self):
		self.store_secret("access_token", "modern-token")
		self.assertEqual(auth.get_access_token(self.setting), "modern-token")

	def test_normalize_shop_url_strips_scheme(self):
		self.assertEqual(auth.normalize_shop_url("https://demo.myshopify.com/"), "demo.myshopify.com")

	def test_validate_settings_requires_client_credentials_when_enabled(self):
		self.setting.client_id = ""
		self.store_secret("client_secret", "")
		with self.assertRaises(frappe.exceptions.ValidationError):
			auth.validate_settings(self.setting)

	@patch("ecommerce_integrations.shopify.auth.requests.post")
	def test_fetch_access_token_uses_client_credentials_grant(self, mock_post):
		self.setting.client_id = "client-id"
		self.store_secret("client_secret", "client-secret")

		response = Mock()
		response.status_code = 200
		response.json.return_value = {
			"access_token": "new-token",
			"scope": "read_orders",
			"expires_in": 86399,
		}
		mock_post.return_value = response

		payload = auth.fetch_access_token(self.setting)

		self.assertEqual(payload["access_token"], "new-token")
		self.assertEqual(self.setting.get_password("access_token"), "new-token")
		self.assertIsNotNone(self.setting.access_token_expires_on)

		# client credentials grant: grant_type is sent and no authorization code is used
		_, kwargs = mock_post.call_args
		self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
		self.assertNotIn("code", kwargs["data"])

	@patch("ecommerce_integrations.shopify.auth.requests.post")
	def test_fetch_access_token_surfaces_app_not_installed(self, mock_post):
		self.setting.client_id = "client-id"
		self.store_secret("client_secret", "client-secret")

		response = Mock()
		response.status_code = 400
		response.json.return_value = {"error": "app_not_installed"}
		response.text = "Oauth error app_not_installed"
		mock_post.return_value = response

		with self.assertRaises(frappe.exceptions.ValidationError):
			auth.fetch_access_token(self.setting)

	@patch("ecommerce_integrations.shopify.auth.fetch_access_token")
	def test_ensure_access_token_refreshes_expired_token(self, mock_fetch):
		self.store_secret("access_token", "old-token")
		self.setting.access_token_expires_on = add_to_date(now_datetime(), seconds=-10)

		auth.ensure_access_token(self.setting)

		mock_fetch.assert_called_once()

	@patch("ecommerce_integrations.shopify.auth.fetch_access_token")
	def test_ensure_access_token_keeps_valid_token(self, mock_fetch):
		self.store_secret("access_token", "good-token")
		self.setting.access_token_expires_on = add_to_date(now_datetime(), hours=10)

		token = auth.ensure_access_token(self.setting)

		mock_fetch.assert_not_called()
		self.assertEqual(token, "good-token")

	@patch("ecommerce_integrations.shopify.auth.fetch_access_token")
	def test_ensure_access_token_refreshes_when_expiry_unknown(self, mock_fetch):
		# carried-over/manual token with no stored expiry, but client credentials exist
		self.store_secret("access_token", "carried-over")
		self.setting.access_token_expires_on = None

		auth.ensure_access_token(self.setting)

		mock_fetch.assert_called_once()

	@patch("ecommerce_integrations.shopify.auth.fetch_access_token")
	def test_ensure_access_token_keeps_token_when_cannot_refresh(self, mock_fetch):
		# no client credentials to refresh with: use the stored token as-is
		self.setting.client_id = ""
		self.store_secret("client_secret", "")
		self.store_secret("access_token", "manual-token")
		self.setting.access_token_expires_on = None

		token = auth.ensure_access_token(self.setting)

		mock_fetch.assert_not_called()
		self.assertEqual(token, "manual-token")

	@patch("ecommerce_integrations.shopify.graphql.requests.post")
	def test_graphql_connection_test(self, mock_post):
		response = Mock()
		response.raise_for_status.return_value = None
		response.json.return_value = {
			"data": {
				"shop": {
					"id": "gid://shopify/Shop/1",
					"name": "Demo Shop",
					"myshopifyDomain": "demo.myshopify.com",
				}
			}
		}
		mock_post.return_value = response

		result = self.setting.test_connection()

		self.assertEqual(result["shop_name"], "Demo Shop")
		self.assertEqual(result["status"], "connected")

	@patch("ecommerce_integrations.shopify.graphql.refresh_access_token", return_value="fresh-token")
	@patch("ecommerce_integrations.shopify.graphql.requests.post")
	def test_graphql_refreshes_and_retries_once_on_401(self, mock_post, mock_refresh):
		unauthorized = Mock()
		unauthorized.status_code = 401

		ok = Mock()
		ok.status_code = 200
		ok.raise_for_status.return_value = None
		ok.json.return_value = {
			"data": {"shop": {"id": "1", "name": "Demo Shop", "myshopifyDomain": "demo.myshopify.com"}}
		}
		mock_post.side_effect = [unauthorized, ok]

		result = self.setting.test_connection()

		self.assertEqual(result["status"], "connected")
		mock_refresh.assert_called_once()
		self.assertEqual(mock_post.call_count, 2)

	@patch("ecommerce_integrations.shopify.graphql.requests.post")
	@patch("ecommerce_integrations.shopify.auth.requests.post")
	def test_test_connection_acquires_token_then_queries_shop(self, mock_token_post, mock_graphql_post):
		# no stored token: Test Connection must mint one via client credentials, then query the shop
		self.setting.client_id = "client-id"
		self.store_secret("client_secret", "client-secret")
		self.store_secret("access_token", "")
		self.setting.access_token_expires_on = None

		token_response = Mock()
		token_response.status_code = 200
		token_response.json.return_value = {"access_token": "minted-token", "expires_in": 86399}
		mock_token_post.return_value = token_response

		shop_response = Mock()
		shop_response.status_code = 200
		shop_response.raise_for_status.return_value = None
		shop_response.json.return_value = {
			"data": {"shop": {"id": "1", "name": "Demo Shop", "myshopifyDomain": "demo.myshopify.com"}}
		}
		mock_graphql_post.return_value = shop_response

		result = self.setting.test_connection()

		self.assertEqual(result["status"], "connected")
		self.assertEqual(result["shop_name"], "Demo Shop")
		mock_token_post.assert_called_once()

	@patch("ecommerce_integrations.shopify.webhooks._delete_webhook")
	@patch("ecommerce_integrations.shopify.webhooks._create_webhook")
	@patch("ecommerce_integrations.shopify.webhooks.list_webhooks")
	def test_sync_webhooks_is_non_destructive_for_existing_topics(
		self, mock_list_webhooks, mock_create_webhook, mock_delete_webhook
	):
		mock_list_webhooks.return_value = [
			ShopifyWebhookSubscription(
				id="gid://shopify/WebhookSubscription/1",
				topic="ORDERS_CREATE",
				uri=connection.get_callback_url(),
			)
		]
		mock_create_webhook.side_effect = lambda client, topic, callback_url: ShopifyWebhookSubscription(
			id=f"gid://shopify/WebhookSubscription/{topic}",
			topic={
				"orders/create": "ORDERS_CREATE",
				"orders/paid": "ORDERS_PAID",
				"orders/fulfilled": "ORDERS_FULFILLED",
				"orders/cancelled": "ORDERS_CANCELLED",
				"orders/partially_fulfilled": "ORDERS_PARTIALLY_FULFILLED",
			}[topic],
			uri=callback_url,
		)

		active = self.setting.sync_webhooks()

		self.assertEqual(active["count"], 5)
		mock_delete_webhook.assert_not_called()

	def test_validate_request_uses_client_secret_for_webhooks(self):
		self.store_secret("client_secret", "client-secret")
		self.setting.shared_secret = ""

		payload = b'{"id": 1}'
		signature = base64.b64encode(hmac.new(b"client-secret", payload, hashlib.sha256).digest()).decode()
		request = SimpleNamespace(data=payload)

		connection._validate_request(request, signature)
