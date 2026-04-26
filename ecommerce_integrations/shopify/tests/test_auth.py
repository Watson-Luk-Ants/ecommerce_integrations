# Copyright (c) 2026, Frappe and Contributors
# See LICENSE

from unittest.mock import Mock, patch

import frappe

from ecommerce_integrations.shopify import auth
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE

from .utils import TestCase


class TestShopifyAuth(TestCase):
	def test_get_valid_access_token_uses_cached_token(self):
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.db_set("access_token", "cached-token", update_modified=False)
		setting.db_set("access_token_expires_on", "2099-01-01 00:00:00", update_modified=False)

		self.assertEqual(auth.get_valid_access_token(), "cached-token")

	@patch("ecommerce_integrations.shopify.auth.requests.post")
	def test_refresh_access_token_fetches_new_token(self, mock_post):
		mock_post.return_value = Mock(
			status_code=200,
			json=lambda: {"access_token": "fresh-token", "expires_in": 3600},
		)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.db_set("access_token", "", update_modified=False)
		setting.db_set("access_token_expires_on", None, update_modified=False)

		self.assertEqual(auth.refresh_access_token(force=True), "fresh-token")
		self.assertEqual(frappe.get_doc(SETTING_DOCTYPE).get_password("access_token"), "fresh-token")

	def test_get_webhook_secret_reads_secret_field(self):
		self.assertEqual(auth.get_webhook_secret(), "supersecret")