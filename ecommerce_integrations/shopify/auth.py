import requests

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from frappe.utils.password import set_encrypted_password

from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	CLIENT_CREDENTIALS_GRANT_TYPE,
	SETTING_DOCTYPE,
)


TOKEN_REQUEST_TIMEOUT = 30

# Client credentials tokens are short-lived (Shopify returns expires_in = 86399s ~ 24h).
# Refresh slightly before the real expiry so in-flight requests never use a dead token.
ACCESS_TOKEN_REFRESH_BUFFER = 300


def normalize_shop_url(shop_url: str | None) -> str:
	if not shop_url:
		return ""

	shop_url = shop_url.strip().removeprefix("https://").removeprefix("http://")
	return shop_url.rstrip("/")


def get_api_version(setting) -> str:
	return getattr(setting, "api_version", None) or API_VERSION


def get_access_token(setting) -> str:
	"""Return the stored access token without refreshing it.

	Use `ensure_access_token` when the token is about to be used for an API call.
	"""
	return _get_password(setting, "access_token")


def has_access_token(setting) -> bool:
	return bool(get_access_token(setting))


def get_webhook_secret(setting) -> str:
	return getattr(setting, "shared_secret", None) or _get_password(setting, "client_secret")


def ensure_access_token(setting) -> str:
	"""Return a usable access token, refreshing it when it is missing or expired.

	The token is acquired through Shopify's client credentials grant whenever the
	stored token is missing or about to expire.
	"""
	if not _token_is_valid(setting):
		fetch_access_token(setting)

	return _get_password(setting, "access_token")


def refresh_access_token(setting) -> str:
	"""Force-acquire a new access token (e.g. after a 401) and return it."""
	fetch_access_token(setting)
	return _get_password(setting, "access_token")


def build_session_auth(setting) -> tuple[str, str, str]:
	shop_url = normalize_shop_url(getattr(setting, "shopify_url", None))
	access_token = ensure_access_token(setting)

	if not shop_url or not access_token:
		frappe.throw(_("Shop URL and a valid Shopify access token are required."))

	return shop_url, get_api_version(setting), access_token


def validate_settings(setting) -> None:
	if not getattr(setting, "enable_shopify", 0):
		return

	if not normalize_shop_url(getattr(setting, "shopify_url", None)):
		frappe.throw(_("Shop URL is required to enable Shopify integration."))

	validate_client_credentials(setting)


def validate_client_credentials(setting) -> None:
	if not getattr(setting, "client_id", None):
		frappe.throw(_("Client ID is required for Shopify authentication."))

	if not _get_password(setting, "client_secret"):
		frappe.throw(_("Client Secret is required for Shopify authentication."))


def fetch_access_token(setting) -> dict:
	"""Acquire a fresh Admin API access token using Shopify's client credentials grant.

	No authorization code or interactive OAuth redirect is required: the app's
	Client ID and Client Secret are exchanged directly for a short-lived token.
	The returned token expires in ~24h, so the expiry is stored and the token is
	transparently refreshed by `ensure_access_token` when needed.

	The app must already be installed on the shop, otherwise Shopify responds with
	`app_not_installed`.

	https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant
	"""
	validate_settings(setting)

	response = requests.post(
		f"https://{normalize_shop_url(setting.shopify_url)}/admin/oauth/access_token",
		data={
			"client_id": setting.client_id,
			"client_secret": _get_password(setting, "client_secret"),
			"grant_type": CLIENT_CREDENTIALS_GRANT_TYPE,
		},
		headers={"Accept": "application/json"},
		timeout=TOKEN_REQUEST_TIMEOUT,
	)

	payload = _parse_token_response(response)

	access_token = payload.get("access_token")
	if not access_token:
		message = payload.get("error_description") or payload.get("error") or _("Shopify token request failed.")
		frappe.throw(message)

	_store_access_token(setting, access_token, payload.get("expires_in"))
	return payload


def get_setting():
	return frappe.get_doc(SETTING_DOCTYPE)


def can_refresh_access_token(setting) -> bool:
	"""True when a fresh token can be minted via the client credentials grant."""
	return bool(getattr(setting, "client_id", None) and _get_password(setting, "client_secret"))


def _token_is_valid(setting) -> bool:
	if not _get_password(setting, "access_token"):
		return False

	expires_on = getattr(setting, "access_token_expires_on", None)
	if not expires_on:
		# Unknown expiry (token carried over from a manual setup, or a response
		# that omitted expires_in). Refresh proactively when we can mint a fresh
		# token; only use it as-is when no client credentials are configured.
		return not can_refresh_access_token(setting)

	return get_datetime() < add_to_date(get_datetime(expires_on), seconds=-ACCESS_TOKEN_REFRESH_BUFFER)


def _store_access_token(setting, access_token: str, expires_in=None) -> None:
	# `access_token` is a Password field: persist it to the auth store, not the
	# table column (Document has no set_password; db_set would store plaintext).
	set_encrypted_password(setting.doctype, setting.name, access_token, "access_token")

	expires_on = add_to_date(now_datetime(), seconds=cint(expires_in)) if expires_in else None
	setting.db_set("access_token_expires_on", expires_on, update_modified=False)

	setting.reload()


def _parse_token_response(response) -> dict:
	try:
		payload = response.json()
	except ValueError:
		payload = {}

	if response.status_code >= 400:
		body = response.text or ""
		if payload.get("error") == "app_not_installed" or "app_not_installed" in body:
			frappe.throw(
				_(
					"Shopify reports the app is not installed on this shop. "
					"Install the app on the store, then acquire the access token again."
				)
			)

		message = (
			payload.get("error_description")
			or payload.get("error")
			or _("Shopify rejected the token request (HTTP {0}).").format(response.status_code)
		)
		frappe.throw(message)

	return payload


def _get_password(setting, fieldname: str) -> str:
	try:
		return setting.get_password(fieldname)
	except Exception:
		frappe.clear_last_message()
		return getattr(setting, fieldname, None) or ""
