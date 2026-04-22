import base64
import functools
import hashlib
import hmac
import json

import frappe
from frappe import _
from shopify.session import Session

from ecommerce_integrations.shopify import webhooks
from ecommerce_integrations.shopify.auth import build_session_auth, get_setting, get_webhook_secret
from ecommerce_integrations.shopify.constants import EVENT_MAPPER
from ecommerce_integrations.shopify.utils import create_shopify_log


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator. The decorator starts a temp session that's destroyed when function returns."""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		setting = get_setting()
		if setting.is_enabled():
			auth_details = build_session_auth(setting)

			with Session.temp(*auth_details):
				return func(*args, **kwargs)

	return wrapper


def get_current_domain_name() -> str:
	return webhooks.get_current_domain_name()


def get_callback_url() -> str:
	return webhooks.get_callback_url()


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event)


def process_request(data, event):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = get_setting()
	secret_key = get_webhook_secret(settings)
	if not secret_key:
		frappe.throw(_("Webhook secret is not configured for Shopify."))

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	if sig != bytes(hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))
