import frappe

from ecommerce_integrations.shopify.constants import API_VERSION, SETTING_DOCTYPE


def execute():
	frappe.reload_doc("shopify", "doctype", "shopify_setting")

	if not frappe.db.get_single_value(SETTING_DOCTYPE, "api_version"):
		frappe.db.set_single_value(SETTING_DOCTYPE, "api_version", API_VERSION)
