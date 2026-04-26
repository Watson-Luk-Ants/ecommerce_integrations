# Ecommerce Integrations Architecture

This document explains how the `ecommerce_integrations` app actually works at runtime: which objects hold configuration, which jobs move data, and how each supported connector maps external events into ERPNext documents.

## Core runtime model

The app is organized around a small set of shared ideas:

- Each integration owns a settings DocType that stores credentials, enablement flags, series defaults, and warehouse or account mappings.
- Frappe hooks are the main orchestration layer. The app does not have one central service object; instead it plugs into ERPNext through document events and scheduled jobs declared in `hooks.py`.
- Cross-system item identity is stored in `Ecommerce Item`. This DocType links an ERPNext `Item` to an external product or SKU and also stores the last inventory sync timestamp.
- Operational traceability is handled mainly through `Ecommerce Integration Log`. Shopify and Unicommerce create one log row per queued or completed sync unit and can retry failed jobs from saved request payloads.
- Shared controllers are intentionally small. `SettingController` defines the warehouse mapping contract, `need_to_run` gates scheduled jobs based on configurable intervals, and `EcommerceCustomer` encapsulates basic ERPNext customer, address, and contact creation.

In practice, the app behaves like a set of connector-specific pipelines built on top of these shared records.

## Global entry points

The highest-value file to read first is `ecommerce_integrations/hooks.py`.

It shows the two global orchestration mechanisms:

- `doc_events`: pushes ERPNext-side changes outward, such as Shopify item uploads or Unicommerce GRN and invoice updates.
- `scheduler_events`: pulls or reconciles data on a schedule, such as Shopify inventory sync, Unicommerce order polling, Amazon order retrieval, and Zenoti invoice or stock syncs.

This means the app is intentionally mixed-mode:

- Some integrations are webhook-driven.
- Some are polling-driven.
- Some also react to local ERPNext document lifecycle events.

## Shared data structures

### Ecommerce Item

`Ecommerce Item` is the canonical item mapping table.

It stores:

- the integration name
- the ERPNext item code
- the external item identifier
- optional variant identifier
- optional SKU
- whether the mapping points to a template item or a variant

Connectors rely on it to answer three questions quickly:

- Has this product already been synced?
- Which ERPNext item should this incoming line item use?
- Which mapped items still need inventory to be pushed?

That mapping layer is what allows inbound order sync to work without duplicating item creation logic inside every order handler.

### Ecommerce Integration Log

`Ecommerce Integration Log` is the main audit trail for Shopify and Unicommerce.

It stores:

- integration name
- method name
- request payload
- response payload
- exception or traceback
- final status such as `Queued`, `Success`, `Invalid`, or `Error`

The retry mechanism works by re-enqueuing the saved `method` with the saved `request_data`. This is why inbound webhook handlers first persist a log row and only then enqueue background work.

## Shopify integration deep dive

Shopify is the most complete bidirectional connector in the app.

It has all of the moving parts at once:

- configuration-driven webhook registration
- inbound webhook processing for orders, payments, fulfillments, and cancellations
- on-demand product creation during order sync
- outbound product publication from ERPNext item events
- scheduled inventory push from ERPNext to Shopify
- a migration path from the older Shopify connector

If you want to understand the architectural style of this app, Shopify is the best connector to study.

### Main files that control Shopify behavior

The control path is concentrated in a small set of modules:

- `shopify/doctype/shopify_setting/shopify_setting.py`: settings, webhooks, warehouse mapping, and custom fields
- `shopify/connection.py`: authenticated Shopify sessions, webhook registration, callback verification, and event dispatch
- `shopify/order.py`: Sales Order creation, tax or shipping mapping, cancellation handling, and historical backfill
- `shopify/invoice.py`: Sales Invoice creation and payment entry generation
- `shopify/fulfillment.py`: Delivery Note creation from fulfillment payloads
- `shopify/product.py`: Shopify product import from inbound orders and Shopify product export from ERPNext items
- `shopify/inventory.py`: scheduled inventory push to Shopify locations
- `shopify/utils.py`: logging and migration from the old connector data model

### Configuration model

`Shopify Setting` is the operational source of truth.

It controls:

- whether the connector is enabled
- shop URL, client ID, client secret, and webhook secret
- cached Shopify Admin access-token metadata used by the auth helper
- company, warehouse, cost center, and default customer choices
- whether Sales Invoices and Delivery Notes should be auto-created
- whether ERPNext items and inventory should be pushed to Shopify
- whether historical orders should be backfilled
- Shopify location to ERPNext warehouse mappings
- Shopify tax mapping defaults and shipping handling options

During validation the settings document does four things that matter at runtime:

- strips the `https://` prefix from the configured shop URL
- registers or unregisters webhooks based on enablement state
- validates that warehouse mappings are usable
- initializes fields such as `last_inventory_sync`

If the connector is enabled, validation also creates the custom fields used to persist Shopify IDs on ERPNext records. That includes Shopify IDs on `Customer`, `Supplier`, `Address`, `Sales Order`, `Delivery Note`, and `Sales Invoice`, plus the Shopify discount field on `Sales Order Item`.

### Session and authentication model

Every code path that talks to Shopify directly is expected to run inside `temp_shopify_session`.

That decorator:

- reads the shop URL from `Shopify Setting`
- gets a valid Shopify Admin access token through the auth helper
- opens a temporary Shopify API session for the duration of the function call
- avoids session setup entirely while tests are running

This keeps Shopify API access localized and makes the connector mostly stateless between requests.

### Webhook setup and lifecycle

Yes. The Shopify module includes webhook setup as part of the `Shopify Setting` save flow. There is no separate webhook installer or background reconciliation job for initial setup.

The owning control path is:

1. `Shopify Setting.validate()` normalizes the shop URL and validates required auth fields.
2. `ShopifySetting._handle_webhooks()` decides whether to register or unregister webhooks.
3. If Shopify is enabled and the local `webhooks` child table is empty, the settings flow forces a token refresh through `auth.refresh_access_token(force=True, setting=self)`.
4. It then calls `connection.register_webhooks(self.shopify_url)`.
5. `register_webhooks()` first removes stale Shopify subscriptions for the current site by calling `unregister_webhooks(shopify_url)`.
6. It opens a temporary Shopify Admin session with `Session.temp(shopify_url, API_VERSION, access_token)`.
7. For each topic in `WEBHOOK_EVENTS`, it creates a Shopify webhook pointing to `get_callback_url()`.
8. On success, the returned Shopify webhook IDs and topics are appended to the `Shopify Setting.webhooks` child table.
9. If registration fails completely, validation throws and the settings save is blocked.

The callback URL is generated by `connection.get_callback_url()` and always targets:

- `https://<current-domain>/api/method/ecommerce_integrations.shopify.connection.store_request_data`

The domain source matters:

- in normal runtime, it uses `frappe.request.host`
- in developer mode, if `localtunnel_url` is present in site config, it uses that instead

That means local webhook setup only works when Shopify can reach the callback host. In practice, local development needs `localtunnel_url` or another publicly reachable tunnel value; otherwise webhook registration may succeed with a callback URL that Shopify cannot deliver to.

Disable-time cleanup is also built in. When Shopify is turned off, `_handle_webhooks()` unregisters matching Shopify subscriptions if enough credential data is present, then clears the local `webhooks` child table.

One important current limitation is that setup is edge-triggered by the local child table state, not by configuration drift. If Shopify is enabled and `webhooks` already contains rows, saving the settings does not automatically re-register webhooks when:

- the callback domain changes
- `localtunnel_url` changes
- credentials are rotated
- Shopify-side subscriptions were deleted manually

Operationally, that means the current module does include webhook setup, but it does not include automatic webhook reconciliation after the initial registration. Today the practical recovery path is to clear the local webhook rows or toggle the integration so `_handle_webhooks()` runs the registration path again.

#### Webhook topics and dispatch

Shopify webhook routing is defined explicitly in `shopify/constants.py`.

The connector subscribes to these topics:

- `orders/create`
- `orders/paid`
- `orders/fulfilled`
- `orders/cancelled`
- `orders/partially_fulfilled`

Those topics are mapped to concrete handler methods:

- `orders/create` -> `sync_sales_order`
- `orders/paid` -> `prepare_sales_invoice`
- `orders/fulfilled` -> `prepare_delivery_note`
- `orders/partially_fulfilled` -> `prepare_delivery_note`
- `orders/cancelled` -> `cancel_order`

The callback flow is always the same:

1. Shopify posts to `store_request_data`.
2. The connector validates the HMAC header with the shared secret.
3. It parses the JSON payload and topic.
4. It creates an `Ecommerce Integration Log` row with the mapped method name and request payload.
5. It enqueues the mapped method on the short queue and passes the log name as `request_id`.

That logging-before-execution step is important because it gives Shopify webhook processing durable auditability and retry capability.

### Inbound order creation flow

`sync_sales_order` is the core inbound order handler.

Its behavior is intentionally idempotent:

- if a `Sales Order` already exists for the Shopify order ID, the handler logs `Invalid` and stops
- otherwise it continues with customer, item, and order creation

The inbound order pipeline is:

1. Set execution user to `Administrator`.
2. Attach the log row to `frappe.flags.request_id`.
3. Build or update the customer state.
4. Ensure all ordered items exist locally.
5. Create the ERPNext Sales Order.
6. Optionally create the Sales Invoice if Shopify says the order is paid.
7. Optionally create the Delivery Note if the order payload already contains fulfillments.

The effective flow is:

`orders/create` webhook -> log row -> queue job -> customer sync -> item sync -> Sales Order -> optional invoice -> optional delivery note

### Customer sync behavior

Customer creation is delegated to `ShopifyCustomer`, which extends the shared `EcommerceCustomer` helper.

That class does more than create a bare `Customer` row:

- it builds the customer name from first name and last name, with email as fallback
- it uses the customer group configured in `Shopify Setting`
- it creates billing and shipping addresses using Shopify address payloads
- it updates existing ERPNext addresses when the customer already exists
- it creates a contact record when the payload contains usable contact data

This is why order sync can assume customer records are already normalized before Sales Order creation starts.

### Item sync behavior during order import

Before a Sales Order is created, `create_items_if_not_exist` walks the order line items and ensures each Shopify product or variant is mapped in `Ecommerce Item`.

If a mapping does not exist, `ShopifyProduct.sync_product` imports the product from Shopify.

That import path handles:

- simple products
- template items with variants
- item attribute creation and attribute-value expansion
- supplier creation from Shopify vendor data
- SKU-based linking to an existing ERPNext item when possible
- creation of the `Ecommerce Item` mapping row after the ERPNext item exists

This item-first behavior is what allows the order pipeline to stay simple later. By the time `create_sales_order` runs, each Shopify line item should already resolve to an ERPNext item code.

### Sales Order creation rules

`create_sales_order` maps the Shopify order into ERPNext with a few notable rules:

- the customer defaults to `default_customer` unless the Shopify customer is already mapped
- line item rates are adjusted for discounts and, when needed, tax-inclusive pricing
- the Sales Order uses the configured naming series or falls back to `SO-Shopify-`
- the order is created with the dummy selling price list and dummy tax category used by the connector
- the connector writes Shopify order ID, order number, and status into custom fields
- the raw Shopify JSON is stored temporarily on the document flags during save

Tax and shipping logic is more involved than a direct field copy.

The connector:

- builds tax rows from line-item tax lines
- can consolidate taxes by account head
- can treat shipping either as a tax row or as a separate item, depending on settings
- looks up tax accounts in the `Shopify Tax Account` child table and falls back to default sales-tax or shipping accounts when configured

If no ERPNext item mapping exists for one of the ordered products, the order creation path treats that as a hard stop rather than silently creating a partial order.

### Sales Invoice creation rules

Invoice creation is split into two layers:

- `prepare_sales_invoice` is the webhook-facing entry point
- `create_sales_invoice` is the actual document-creation routine

An ERPNext `Sales Invoice` is created only when all of these conditions hold:

- no invoice already exists for the Shopify order ID
- the Sales Order is submitted
- the Sales Order is not already fully billed
- `sync_sales_invoice` is enabled in `Shopify Setting`

When an invoice is created, the connector:

- uses `make_sales_invoice` from ERPNext
- stamps Shopify order ID and order number on the invoice
- sets posting and due dates from the Shopify order date
- applies the configured naming series or falls back to `SI-Shopify-`
- sets item cost centers from the Shopify settings
- auto-creates and submits a `Payment Entry` when the invoice total is positive

So for paid Shopify orders, the connector is not just creating an invoice shell. It also closes the payment loop inside ERPNext.

### Delivery Note and fulfillment behavior

Fulfillment handling is parallel to invoice handling.

`prepare_delivery_note` is the webhook-facing entry point, and `create_delivery_note` does the actual ERPNext document creation.

A `Delivery Note` is created per fulfillment only when:

- no `Delivery Note` already exists for that Shopify fulfillment ID
- the related Sales Order exists and is submitted
- `sync_delivery_note` is enabled

When creating the document, the connector:

- uses ERPNext's `make_delivery_note`
- stamps Shopify order ID, order number, and fulfillment ID into custom fields
- sets the posting date from the fulfillment timestamp
- applies the configured naming series or falls back to `DN-Shopify-`
- remaps fulfillment items to ERPNext warehouses using the Shopify location to ERPNext warehouse mapping

That last point matters because fulfillment location drives the warehouse assigned on Delivery Note items. The warehouse mapping is not only for inventory push; it also affects inbound fulfillment materialization.

### Cancellation behavior

`cancel_order` handles `orders/cancelled` webhooks.

The cancellation logic is conservative:

- if the Sales Order does not exist, the event is logged as `Invalid`
- if a linked Sales Invoice exists, the connector updates the stored Shopify order status on the invoice
- if linked Delivery Notes exist, it updates the stored Shopify order status on them too
- if no invoice and no delivery notes exist, the submitted Sales Order itself is cancelled
- otherwise the Sales Order stays active and only its stored Shopify status field is updated

So Shopify cancellations do not blindly cancel local ERPNext transactions that may already have downstream accounting or stock impact.

### Outbound product sync from ERPNext

Shopify is also updated when ERPNext items change.

The `Item.after_insert` and `Item.on_update` hooks call `upload_erpnext_item`.

That outbound path has several guardrails. It exits early when:

- the item was created by the integration itself
- Shopify is disabled
- item upload is disabled in settings
- the current operation is an import
- the item is a template item with variants
- the item has more than three attributes
- the item is a variant but variant upload is disabled

If the product does not yet exist in Shopify, the connector creates it and then creates one or more `Ecommerce Item` mappings locally.

If the product already exists and `update_shopify_item_on_update` is enabled, the connector updates the Shopify product instead.

This means Shopify product publication is ERPNext-driven, but only for items that fit Shopify's constraints and the connector's supported variant model.

### Scheduled inventory push

Inventory synchronization is outbound-only in the current Shopify connector.

The scheduled job `update_inventory_on_shopify` runs from the global scheduler and only proceeds when:

- Shopify is enabled
- stock-level updates are enabled in settings
- `need_to_run` says the configured interval has elapsed

The job then:

- loads ERPNext to Shopify warehouse mappings
- collects inventory levels for mapped items through the shared inventory controller
- batches updates in groups of 50
- resolves each Shopify variant to its Shopify inventory item ID
- pushes available quantity as `actual_qty - reserved_qty`
- updates `inventory_synced_on` for successful rows and even for rows where Shopify says the variant or location no longer exists

The connector writes a summary log per batch with statuses such as `Success`, `Partial Success`, `Failed`, or `Not Found`.

One subtle but important behavior here is that missing Shopify variants or locations are treated as ignored end states rather than retry storms. The mapping row is marked as synced and the batch moves on.

### Historical backfill and recovery

Shopify is not fully dependent on live webhooks.

There is an hourly `sync_old_orders` path that can backfill orders for a configured date range when `sync_old_orders` is enabled on `Shopify Setting`.

That job:

- fetches older Shopify orders through an authenticated Shopify session
- creates a fresh log row per order
- reuses the normal `sync_sales_order` flow for each fetched order
- disables the `sync_old_orders` flag after the run completes

This is effectively a replay mechanism built on top of the normal order-import code path, not a separate importer.

### Logging, retries, and observability

Shopify uses the shared `Ecommerce Integration Log` consistently.

Handlers generally report one of four outcomes:

- `Success`
- `Invalid`
- `Error`
- inventory-specific batch summaries such as `Partial Success`

Because webhook payloads are stored with the log row, failed inbound jobs can be retried using the generic retry mechanism in the shared log DocType.

This gives Shopify better operational ergonomics than connectors that only write unstructured error logs.

### Migration from the old Shopify connector

The app still contains a migration bridge for older Shopify installations.

`Shopify Setting.on_update` triggers `migrate_from_old_connector` when the new connector is enabled and old data has not yet been migrated.

That migration path:

- requires the old Shopify connector to be disabled first
- scans existing `Item` records for legacy Shopify product and variant fields
- creates `Ecommerce Item` mappings for rows that have old Shopify identifiers but no new mapping yet
- marks the migration complete once the mapping transfer succeeds

This matters because the newer connector expects `Ecommerce Item` to be the canonical item-link table.

### Operational summary

The Shopify connector can be understood as four cooperating loops:

- webhook ingestion for orders, payments, fulfillments, and cancellations
- ERPNext-driven outbound product publication
- scheduled outbound inventory push
- one-time or ad hoc historical backfill and migration

When Shopify behavior is wrong, the fastest debugging order is:

1. Check `Shopify Setting` enablement, credentials, and webhook registration state.
2. Check whether the callback URL matches the reachable site URL or `localtunnel_url`.
3. Check the latest `Ecommerce Integration Log` rows for the relevant handler.
4. Check whether the required `Ecommerce Item` mappings exist.
5. Check whether warehouse and tax mappings are complete.
6. Check whether the relevant automation is webhook-driven, item-hook-driven, or scheduler-driven.

### Alignment with Shopify client-credentials guidance

This section compares the current connector with Shopify's official client-secret guidance:

- https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets

#### Findings

The current implementation is only partially aligned.

What already aligns:

- The connector uses a separate app secret field, `shared_secret`, for webhook HMAC verification.
- Incoming webhooks are validated against the `X-Shopify-Hmac-Sha256` header before any business logic runs.
- The API token used for Shopify Admin calls is stored separately from the webhook verification secret.

What does not align with the official client-credentials model:

- The connector does not model `client_id` at all.
- The connector does not request access tokens from Shopify's OAuth endpoint using `grant_type=client_credentials`.
- The connector assumes a manually provisioned long-lived `password / access token` field and uses it directly for all Admin API sessions.
- The connector does not track access-token expiry, even though the official flow returns a time-limited token.
- The connector does not automatically re-fetch a token when the current token expires or is revoked.
- The connector does not provide a secret-rotation workflow for the app secret or API credentials.
- The `shared_secret` field is stored as plain Data rather than a masked secret field, which is weaker than the handling expected for a credential that signs webhook payloads.

The practical consequence is that the module behaves more like a manually configured custom-app token integration than an implementation of Shopify's documented client-credentials flow.

#### Code-level gap summary

The misalignment is concentrated in a few places:

- `Shopify Setting` stores `password / access token` and `shared_secret`, but no `client_id`, token expiry, or token-refresh metadata.
- `temp_shopify_session` opens Shopify API sessions directly from the stored token.
- webhook registration and unregistration also use that stored token directly.
- webhook verification uses the app secret correctly, but secret storage and rotation are not formalized.

#### Update plan

To align the module with Shopify's official guidance under a flag-day rewrite, the module should cut over to the new credential model in one coordinated release and remove the legacy static-token path entirely.

This means the rewrite should assume:

- the current `password / access token` model will be removed, not preserved
- all Shopify API sessions will be created from freshly obtained client-credentials tokens
- existing sites must update configuration before or at deployment time
- webhook verification must continue to work immediately after cutover

##### Workstream 1: Replace the credential model

Update `Shopify Setting` so the connector stores the credentials required by the official flow.

Add fields for:

- `client_id`
- `client_secret` as a masked secret field
- `access_token`
- `access_token_expires_on`
- optional metadata such as `last_token_refresh_on`

Also update the webhook secret handling surface:

- convert `shared_secret` to a masked secret field
- decide whether `shared_secret` remains a distinct field or is replaced by `client_secret` as the webhook-signing secret source
- remove the legacy `password / access token` field from the supported configuration contract

The key difference from the staged plan is that there is no compatibility mode. After cutover, the connector should require the new fields.

##### Workstream 2: Centralize token acquisition

Create one Shopify auth helper that owns token lifecycle.

That helper should:

- request a token from `POST https://{shop}.myshopify.com/admin/oauth/access_token`
- send `grant_type=client_credentials`, `client_id`, and `client_secret`
- persist the returned token and expiry timestamp
- refresh the token before any Shopify Admin API call when the current token is missing or expired
- optionally retry once on Shopify authentication failures that indicate token expiry or revocation

After this rewrite, no business logic module should read credentials directly from ad hoc fields except through this helper.

##### Workstream 3: Route every Shopify session through the token helper

Refactor the session-entry points to consume the centralized token provider.

This includes:

- `temp_shopify_session`
- `register_webhooks`
- `unregister_webhooks`
- any other direct `Session.temp(...)` usage in the Shopify module

The goal is that every Admin API session is created from a freshly validated short-lived token and there is no remaining static-token path anywhere in the Shopify module.

##### Workstream 4: Preserve webhook verification while hardening secret handling

Keep the current webhook validation behavior, because the HMAC verification model is already correct.

Update only the storage and maintenance model:

- store the webhook-signing secret as a masked secret
- document that it must match the app's current client secret or webhook signing secret source in Shopify
- add an explicit revalidation step for webhook delivery after secret rotation

This workstream should not change the webhook control flow; it should only harden the secret-management surface and ensure the rewrite does not break webhook trust validation.

##### Workstream 5: Add rotation and failure-recovery behavior

The official guidance explicitly calls out secret rotation, so the module should treat rotation as a first-class lifecycle event.

Add support for:

- re-fetching access tokens after client secret changes
- invalidating cached tokens when credentials change
- surfacing a clear configuration error when webhook verification starts failing after secret rotation
- optionally providing an admin action to force token refresh and webhook re-registration

This closes the operational gap between a one-time setup and a maintainable long-running integration.

##### Workstream 6: Define the flag-day deployment contract

Because this is a flag-day rewrite, deployment expectations need to be explicit.

The release should ship with:

- a schema update that adds the new credential fields and removes the legacy field from active use
- a validation error that blocks enabling Shopify until `client_id` and `client_secret` are configured
- release notes that clearly state the old static-token setup is no longer supported
- an upgrade checklist telling operators to populate new credentials before enabling the connector after deployment
- a one-time post-upgrade verification checklist for webhook delivery, token acquisition, and API access

This accepts a coordinated operational cutover instead of trying to keep both models alive in code.

##### Workstream 7: Update tests and documentation

The code change is not complete until the operational contract is documented and testable.

Required test additions:

- token acquisition success and failure cases
- token reuse before expiry and re-fetch after expiry
- webhook verification with the configured client secret
- behavior after secret rotation or revoked token scenarios

Required documentation updates:

- replace `Password / Access Token` setup guidance with `client_id` and `client_secret` guidance only
- explain that Admin API tokens are obtained automatically and are time-limited
- document how to rotate client credentials safely
- document the flag-day upgrade procedure for existing static-token installations

#### Recommended implementation order

If the module is actually going to be rewritten as a flag-day cutover, this is the lowest-risk execution order:

1. Add new settings fields and secret storage changes.
2. Implement centralized token acquisition and caching.
3. Refactor Shopify session creation to use that helper.
4. Keep webhook verification behavior intact, but move its secret to masked storage.
5. Remove the legacy static-token code path and make the new credentials mandatory.
6. Update tests.
7. Update user-facing setup documentation and release notes.

That ordering still reduces the chance of breaking order sync, webhook processing, and product or inventory sync all at once, but it does so within a single coordinated release instead of a compatibility-first migration.

## Unicommerce flow

Unicommerce is a hybrid connector: it polls for new external activity and also pushes ERPNext-side changes back when certain documents change.

### Configuration and setup

`Unicommerce Settings` stores:

- connector enablement
- OAuth credentials and rotating access tokens
- ERPNext to facility warehouse mappings
- default series and behavior flags
- feature toggles such as auto GRN, item upload, order sync, and inventory sync behavior

On validation it:

- refreshes tokens if needed
- validates one-to-one warehouse mappings
- prepares GRN-related dependencies when auto GRN is enabled
- creates the custom fields used on `Item`, `Sales Order`, `Sales Invoice`, `Delivery Note`, and related child tables

This connector treats token renewal as part of normal runtime, not as a one-time setup step.

### Inbound order polling

The core inbound job is `sync_new_orders`, scheduled every five minutes.

The sequence is:

1. Read `Unicommerce Settings` and stop immediately if disabled.
2. Use `need_to_run` with the configured order sync frequency.
3. Poll Unicommerce for recently updated orders.
4. Filter results to enabled `Unicommerce Channel` records.
5. Fetch full order details per candidate order.
6. Ensure every ordered SKU is already mapped; if not, import that product first.
7. Create the ERPNext Sales Order with external order code, facility code, channel, status, taxes, and mapped line items.
8. If the installation is configured to only sync completed orders, create Sales Invoices immediately from shipping package invoice data.

This means Unicommerce order sync is item-first. Orders are not created until the required SKU mappings exist.

### Outbound updates from ERPNext

Several ERPNext document events push data back to Unicommerce:

- `Stock Entry.on_submit` uploads GRN information.
- `Sales Invoice.on_submit` and `on_cancel` synchronize invoice state.
- `Sales Order.on_update_after_submit` can update shipping package information when package type changes.
- `Sales Order.on_cancel` prevents downstream pick-list inconsistencies.

There are also scheduled outbound jobs:

- `upload_new_items` sends newly marked ERPNext items to Unicommerce.
- `update_inventory_on_unicommerce` pushes inventory deltas using warehouse mappings.
- `update_sales_order_status` and `update_shipping_package_status` pull newer state back into ERPNext after fulfillment advances externally.
- `prepare_delivery_note` creates delivery notes from ready packages.

So Unicommerce is not just a one-way importer. It is a continuous reconciliation loop between ERPNext and the external OMS or WMS.

### Operational helpers

Unicommerce exposes explicit force-sync entry points for items, orders, and inventory. Those helpers simply enqueue the same underlying methods with `force=True`, which is useful because it keeps manual sync behavior identical to scheduled sync behavior.

## Amazon flow

Amazon is currently the most pull-oriented connector in the app.

### Configuration and setup

`Amazon SP API Settings` stores:

- SP-API credentials
- company and account defaults
- field mapping rules used to find or create ERPNext items
- retry limits
- the `after_date` window used for order fetches

Validation ensures:

- credentials are valid
- field mappings point to real and, when required, unique `Item` fields
- the fetch window stays within the last 30 days
- the custom Sales Order field for the Amazon order ID exists

### Scheduled order retrieval

The main hourly job is `schedule_get_order_details`.

It loops through active and sync-enabled Amazon settings records and calls into `AmazonRepository` to pull orders from the SP-API.

`AmazonRepository` is the main adapter layer. It is responsible for:

- instantiating SP-API clients
- retrying failed calls up to the configured limit
- disabling sync automatically when retry exhaustion suggests a persistent credential or API problem
- resolving or creating ERPNext items from Amazon order items
- creating `Ecommerce Item` links for created items
- building extra charge and fee rows from Amazon financial events

Compared with Shopify and Unicommerce, Amazon keeps more logic inside one repository object and relies less on shared logging infrastructure.

### Item resolution strategy

Amazon order import starts by deciding how an incoming order item maps to an ERPNext `Item`.

That behavior is controlled by the field mapping child table on `Amazon SP API Settings`:

- exactly one mapped field must be marked as the item lookup key
- if the lookup fails and `create_item_if_not_exists` is enabled, the repository creates the ERPNext item, item price, and `Ecommerce Item` mapping automatically

That lookup contract is the core of the Amazon connector. Without it, order import cannot safely attach line items.

## Zenoti flow

Zenoti is the most batch-oriented connector in the app.

It is centered on `Zenoti Settings` and `Zenoti Center` records rather than on webhooks.

### Configuration and setup

`Zenoti Settings` validates the API key and prepares ERPNext for the sync model by:

- creating required custom fields
- creating supporting masters such as genders, item groups, and a `Tips` item
- ensuring a payment mode exists for gift and prepaid card handling
- checking for an opening stock reconciliation
- disabling perpetual inventory for the configured company if needed

This is a strong signal that the Zenoti connector assumes accounting and stock behavior different from a typical ERPNext perpetual-inventory setup.

### Center-based synchronization model

Zenoti data is partitioned by center.

`Zenoti Center` provides manual sync entry points for:

- employees
- customers
- items
- categories
- sales invoices for a date range
- stock reconciliation for a date

That center abstraction is important because ERPNext warehouse and cost center mappings are configured per center and are required before transactional sync can succeed.

### Scheduled transactional sync

The scheduled jobs are:

- `sync_invoices` in `hourly_long`
- `sync_stocks` in `daily_long`

`sync_invoices` iterates configured centers, checks the last sync timestamp against the configured interval, then builds ERPNext Sales Invoices from Zenoti sales reports.

`sync_stocks` processes two stock-side workloads per center:

- create `Stock Reconciliation` entries from Zenoti inventory snapshots
- create purchase records from Zenoti purchase orders and returns

So the Zenoti connector is effectively a batch importer for sales, stock positions, purchases, suppliers, and master data.

### Error handling model

Unlike Shopify and Unicommerce, Zenoti mostly writes failures into `Zenoti Error Logs` instead of the shared `Ecommerce Integration Log`. That reflects the connector's more report-oriented, batch-processing style.

## How the connectors differ

Although all four integrations live in one app, they do not share the same operating model.

- Shopify: webhook-first with scheduled backfill and outbound product or inventory push.
- Unicommerce: frequent polling plus ERPNext event-driven push-backs for fulfillment and GRN flows.
- Amazon: scheduled order ingestion with repository-managed retries and item creation.
- Zenoti: center-based batch import for invoices, stock, purchases, and master data.

Understanding those differences matters because operational issues usually come from the orchestration mode, not from the individual field mapping.

Examples:

- Missing Shopify data is often a webhook registration, callback URL, or signature problem.
- Missing Unicommerce data is often a frequency gate, token, channel, or warehouse-mapping issue.
- Missing Amazon data is often an SP-API credential, retry exhaustion, or item lookup mapping issue.
- Missing Zenoti data is often a center mapping, interval, or prerequisite master-data issue.

## Where to start reading the code

If you need to trace behavior quickly, this reading order is the most effective:

1. `ecommerce_integrations/hooks.py` for the top-level triggers.
2. The relevant settings DocType for the connector you care about.
3. The connector's primary order or transaction module.
4. `Ecommerce Item` to understand identity mapping.
5. The connector log helper or error log path.

For each connector, the best first files are usually:

- Shopify: `shopify/doctype/shopify_setting/shopify_setting.py`, `shopify/connection.py`, `shopify/order.py`
- Unicommerce: `unicommerce/doctype/unicommerce_settings/unicommerce_settings.py`, `unicommerce/order.py`, `unicommerce/utils.py`
- Amazon: `amazon/doctype/amazon_sp_api_settings/amazon_sp_api_settings.py`, `amazon/doctype/amazon_sp_api_settings/amazon_repository.py`
- Zenoti: `zenoti/doctype/zenoti_settings/zenoti_settings.py`, `zenoti/doctype/zenoti_center/zenoti_center.py`, `zenoti/sales_transactions.py`

## Practical debugging checklist

When a sync is not behaving as expected, check in this order:

1. Is the integration enabled in its settings DocType?
2. Are credentials or tokens still valid?
3. Are warehouse, company, channel, or center mappings complete?
4. Did the relevant scheduler or document event actually fire?
5. Was a connector log row or Zenoti error log created?
6. Does the required `Ecommerce Item` mapping already exist?

That sequence matches how the code actually fails in production: configuration first, orchestration second, mapping third.