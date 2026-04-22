# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Frappe/ERPNext app** (installed via [bench](https://github.com/frappe/bench)) that syncs ERPNext with external ecommerce platforms: **Shopify, Unicommerce, Amazon, and Zenoti**. It is not a standalone Python package — it runs inside a Frappe `bench` against a Frappe site, and almost everything (DocTypes, hooks, background jobs, ORM) goes through the Frappe framework. `required_apps = ["frappe/erpnext"]`.

## Commands

This app cannot run on its own; commands run from the `frappe-bench` directory against an installed site. There is no local `package.json`/`Makefile`.

```bash
# Lint / format (run from repo root; configured in .pre-commit-config.yaml + pyproject.toml)
pre-commit run --all-files
ruff check ecommerce_integrations          # lint
ruff format ecommerce_integrations         # format

# Tests (run from ~/frappe-bench, against the test site)
bench --site test_site run-tests --app ecommerce_integrations           # whole app
bench --site test_site run-tests --module ecommerce_integrations.shopify.tests.test_order   # one module
bench --site test_site run-tests --doctype "Ecommerce Item"             # one doctype's tests
bench --site test_site run-parallel-tests --app ecommerce_integrations  # what CI runs

# Apply schema/data migrations after changing patches.txt or fixtures
bench --site test_site migrate
```

CI (`.github/helper/install.sh`) builds a fresh bench with frappe + erpnext + payments, installs this app onto `test_site`, then runs parallel tests. Tests need a real MariaDB-backed Frappe site; `before_tests` (`utils/before_test.py`) bootstraps a company and tax accounts.

Note: pre-commit's `no-commit-to-branch` blocks **direct commits to `develop`** — work on a feature branch. Per README, PRs target `develop`, never `main` (release branch).

## Architecture

### Frappe conventions (read these first if new to Frappe)
- **`hooks.py`** is the wiring hub: `doc_events` (ERPNext document lifecycle → integration handlers), `scheduler_events` (cron/periodic syncs), `before_tests`, JS overrides. When you add a sync trigger, it is registered here, not by importing.
- **DocTypes** are the data model. Each lives in `<module>/doctype/<name>/` as a `.json` schema + `.py` controller class. Settings for each integration are DocTypes (e.g. `Shopify Setting`, `Unicommerce Settings`, `Zenoti Settings`, `Amazon SP API Settings`).
- **`patches.txt`** lists idempotent migration scripts in `patches/`, run on `bench migrate`. Custom fields on ERPNext DocTypes (Item, Sales Order, …) are created this way.

### Cross-integration core (`ecommerce_integrations/`)
- **`Ecommerce Item`** DocType (`ecommerce_integrations/doctype/ecommerce_item/`) is the central mapping: it links one ERPNext `Item` to a product on an integration, keyed by `(integration, integration_item_code, variant_id, sku)`. Use its module functions — `is_synced`, `get_erpnext_item`, `create_ecommerce_item` — rather than querying the table directly. `inventory_synced_on` on this record is how inventory deltas are detected.
- **`Ecommerce Integration Log`** DocType logs every inbound webhook / sync attempt with status (`Queued`/`Success`/`Error`) and supports retry (`resync`, `bulk_retry`). Background jobs are keyed to a log via `request_id`/`frappe.flags.request_id`.
- **`controllers/`** holds the shared abstractions every integration builds on:
  - `setting.py` — `SettingController` base class (warehouse mapping + `is_enabled` interface) that integration Settings DocTypes subclass.
  - `inventory.py` — warehouse→`Bin` inventory-level queries used by all inventory sync jobs (handles group/leaf warehouse consolidation).
  - `scheduling.py` — `need_to_run(setting, interval_field, timestamp_field)` makes scheduler events user-configurable by interval.
  - `customer.py` — shared customer sync helpers.

### The standard sync pattern (applies to every integration)
1. **Inbound (webhook):** a whitelisted endpoint validates an HMAC signature, creates an `Ecommerce Integration Log`, looks up the handler in an `EVENT_MAPPER` dict, and `frappe.enqueue`s it as a background job. See `shopify/connection.py::store_request_data` + `shopify/constants.py::EVENT_MAPPER`.
2. **Outbound / scheduled:** `scheduler_events` in `hooks.py` call sync functions (e.g. `shopify.inventory.update_inventory_on_shopify`, `unicommerce.order.sync_new_orders`) that read ERPNext data via `controllers/` and push to the integration's API client.
3. Each integration package follows the same file layout: `constants.py` (DocType names, custom field names, event maps), an API client / `connection.py`, and per-entity modules: `order.py`, `product.py`, `inventory.py`, `invoice.py`, `customer.py`, `fulfillment.py`/`delivery_note.py`.

### Integration-specific notes
- **Shopify** (`shopify/`) authenticates with `Client ID` + `Client Secret` via Shopify's **client credentials grant** (`auth.py`): `fetch_access_token` exchanges them for a short-lived (~24h) Admin API token, and `ensure_access_token` transparently refreshes it before expiry (the token + `access_token_expires_on` live on `Shopify Setting`). The app must be installed on the shop first or Shopify returns `app_not_installed`. Connection testing and **webhook sync use the GraphQL Admin API** (`graphql.py`, `webhooks.py`); order/product/inventory sync still use the REST compatibility layer via the `ShopifyAPI` package. Any function hitting the Shopify API must be wrapped with the `@temp_shopify_session` decorator (`connection.py`). Webhooks are synced explicitly (not destructively recreated on every settings save).
- **Unicommerce** (`unicommerce/`) centers on `UnicommerceAPIClient` (`api_client.py`), a thin `requests` wrapper with token renewal; it has the richest ERPNext document flow (GRN, pick lists, delivery notes, shipment manifests) wired through `doc_events`.
- **Amazon** (`amazon/`) and **Zenoti** (`zenoti/`) are DocType-driven: most logic lives under their `doctype/.../*.py` settings controllers and is driven by `scheduler_events`.

## Conventions
- **Ruff** (`pyproject.toml`): line length 110, **tab indentation**, double quotes. Many lint rules are intentionally relaxed (`F401`, `E501`, star-import rules) because Frappe relies on `import *` and dynamic names — don't "fix" these.
- Reach for `frappe.qb` (query builder) / `frappe.db` and `frappe.get_doc` rather than raw SQL or ORM-bypassing writes; raw `frappe.db.sql` exists but is the exception.
- Log integration failures through `create_log` / the per-integration `create_*_log` helpers so they surface in `Ecommerce Integration Log` and remain retryable — don't let exceptions in background jobs vanish.
- Target runtime is Python 3.10+ (`requires-python`); CI runs the test/lint matrix on 3.14.
