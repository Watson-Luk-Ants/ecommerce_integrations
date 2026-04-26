from __future__ import annotations

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log

TOKEN_REFRESH_BUFFER_SECONDS = 300


def get_valid_access_token() -> str:
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	if _token_is_missing_or_expired(setting):
		return refresh_access_token(setting=setting)

	return setting.get_password("access_token")


def refresh_access_token(force: bool = False, setting=None) -> str:
	setting = setting or frappe.get_doc(SETTING_DOCTYPE)
	if not force and not _token_is_missing_or_expired(setting):
		return setting.get_password("access_token")

	response = _request_access_token_from_shopify(setting)
	access_token = response["access_token"]
	expires_in = int(response.get("expires_in") or 0)
	_persist_token(setting, access_token, expires_in)
	return access_token


def invalidate_cached_access_token(setting=None) -> None:
	setting = setting or frappe.get_doc(SETTING_DOCTYPE)
	setting.db_set("access_token", "", update_modified=False)
	setting.db_set("access_token_expires_on", None, update_modified=False)


def get_webhook_secret() -> str:
	setting = frappe.get_doc(SETTING_DOCTYPE)
	secret = setting.get_password("shared_secret")
	if not secret:
		frappe.throw(_("Shopify webhook secret is not configured."))
	return secret


def _token_is_missing_or_expired(setting) -> bool:
	access_token = setting.get_password("access_token")
	if not access_token:
		return True

	access_token_expires_on = setting.access_token_expires_on
	if not access_token_expires_on:
		return True

	return get_datetime(access_token_expires_on) <= now_datetime()


def _request_access_token_from_shopify(setting) -> dict:
	client_secret = setting.get_password("client_secret")
	if not (setting.client_id and client_secret):
		frappe.throw(_("Shopify client credentials are not configured."))

	url = f"https://{setting.shopify_url}/admin/oauth/access_token"
	response = requests.post(
		url,
		data={
			"grant_type": "client_credentials",
			"client_id": setting.client_id,
			"client_secret": client_secret,
		},
		timeout=30,
	)

	if response.status_code != 200:
		create_shopify_log(
			status="Error",
			method="shopify.auth.refresh_access_token",
			response_data=response.text,
			message="Failed to fetch Shopify access token.",
		)
		frappe.throw(_("Failed to fetch Shopify access token."))

	return response.json()


def _persist_token(setting, access_token: str, expires_in: int) -> None:
	setting.db_set("access_token", access_token, update_modified=False)
	setting.db_set(
		"access_token_expires_on",
		add_to_date(now_datetime(), seconds=max(expires_in - TOKEN_REFRESH_BUFFER_SECONDS, 0)),
		update_modified=False,
	)
	setting.db_set("last_token_refresh_on", now_datetime(), update_modified=False)