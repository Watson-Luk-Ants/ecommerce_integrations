# Shopify Client-Credentials Rewrite Spec

This document defines the implementation for rewriting the Shopify connector to align with Shopify's official client-credentials guidance.

Reference:

- https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets

This spec assumes a flag-day cutover. The legacy static `password / access token` model is removed as an active authentication path.

## Scope

This spec applies to the Shopify connector under `ecommerce_integrations/shopify/` and to the `Shopify Setting` DocType.

The rewrite covers:

- credential storage changes
- token acquisition and refresh behavior
- Shopify Admin API session creation
- webhook secret handling
- runtime validation and failure behavior
- migration and deployment expectations for a hard cutover
- tests and documentation updates

## Goals

- Replace the legacy static Admin token model with Shopify's client-credentials token flow.
- Ensure all Shopify Admin API access goes through one token provider.
- Keep webhook verification correct across the rewrite.
- Make token expiry and credential rotation first-class operational concerns.
- Remove ambiguous credential semantics from `Shopify Setting`.

## Non-goals

- No compatibility mode for the legacy token path.
- No partial rollout behind a runtime feature toggle.
- No changes to Unicommerce, Amazon, or Zenoti.
- No redesign of the business flows for orders, invoices, fulfillments, products, or inventory beyond auth and credential usage.

## Current-state summary

The current Shopify connector:

- stores a `password / access token` value in `Shopify Setting`
- opens Shopify sessions directly from that stored token
- uses `shared_secret` for webhook HMAC verification
- has no `client_id`
- does not acquire tokens dynamically
- does not track token expiry
- does not formalize credential rotation behavior

The rewrite changes only the authentication and secret-management model, not the higher-level business flows.

## Target-state summary

After the rewrite:

- `Shopify Setting` stores durable credentials only
- access tokens are acquired from Shopify on demand
- access tokens are cached with expiry metadata
- every Shopify Admin API session is created through a shared token helper
- webhook verification continues to use the signing secret
- the legacy static token field is not used anywhere in the Shopify code path

## Settings schema

## New credential model

`Shopify Setting` must support these fields:

- `client_id`
- `client_secret`
- `access_token`
- `access_token_expires_on`
- `last_token_refresh_on`

Recommended field behavior:

- `client_id`: Data, mandatory when Shopify is enabled
- `client_secret`: Password, mandatory when Shopify is enabled
- `access_token`: Password or hidden secret storage field, managed by code only
- `access_token_expires_on`: Datetime, managed by code only
- `last_token_refresh_on`: Datetime, managed by code only

Webhook secret handling:

- `shared_secret` should be converted from `Data` to `Password`
- if the connector can use `client_secret` directly for webhook validation, document that explicitly and remove duplication only if the product behavior is verified first
- otherwise keep `shared_secret` as an explicit webhook-signing field with clear labeling

Legacy field handling:

- `password / access token` remains in schema only long enough to support patching and operator visibility during the release window
- the field must not be read by runtime auth code after the rewrite lands
- after the release stabilizes, the field can be removed in a later cleanup patch if desired

## Validation rules

When `enable_shopify` is true:

- `shopify_url` is required
- `client_id` is required
- `client_secret` is required
- `shared_secret` is required if it remains distinct from `client_secret`
- `company`, warehouse, and existing business settings retain their current validation behavior

Validation must fail fast when the connector is enabled without the new credentials.

## Authentication design

## New auth helper

Create a dedicated auth module, for example:

- `ecommerce_integrations/shopify/auth.py`

This module owns the entire token lifecycle.

Suggested public functions:

- `get_valid_access_token()`
- `refresh_access_token(force=False)`
- `invalidate_cached_access_token()`
- `get_webhook_secret()`

Suggested internal helpers:

- `_token_is_missing_or_expired()`
- `_token_is_near_expiry(buffer_seconds=300)`
- `_request_access_token_from_shopify()`
- `_persist_token(access_token, expires_in)`

## Token acquisition contract

The helper must request an access token from Shopify using:

- `POST https://{shop}.myshopify.com/admin/oauth/access_token`
- `grant_type=client_credentials`
- `client_id`
- `client_secret`

The helper must:

- parse the response body
- persist `access_token`
- persist `access_token_expires_on` using the returned `expires_in`
- persist `last_token_refresh_on`
- return the access token to the caller

## Token caching contract

The access token is cached state, not primary configuration.

Rules:

- if there is no cached token, fetch one
- if the token is expired, fetch one
- if the token is near expiry, fetch one
- otherwise reuse the cached token

Use an early-refresh buffer of 300 seconds by default.

Rationale:

- avoids edge-case failures at exact expiry
- reduces race conditions for concurrent workers
- keeps short-lived sessions stable during long-running sync work

## Failure handling contract

If Shopify rejects an API call due to token invalidity:

1. invalidate the cached token
2. fetch a new token
3. retry the original operation once

If the retry also fails due to authentication:

- surface a configuration error
- log the failure clearly
- do not loop indefinitely

This module must not silently swallow repeated auth failures.

## Session creation design

## Replace direct Session.temp credential usage

Every direct `Session.temp(...)` call in the Shopify module must route through the token helper.

Primary targets:

- `shopify/connection.py`
- any session construction in `product.py`, `inventory.py`, `order.py`, or related helpers if present later

Target pattern:

1. get a valid token from `auth.py`
2. build `Session.temp(shop_url, api_version, access_token)`
3. execute Shopify API work inside that session

The business code must not decide whether to refresh tokens. That decision belongs only to the auth helper.

## Required code changes by file

## `shopify/doctype/shopify_setting/shopify_setting.json`

Required changes:

- add `client_id`
- add `client_secret`
- add `access_token`
- add `access_token_expires_on`
- add `last_token_refresh_on`
- convert `shared_secret` to a secret field
- relabel the authentication section so it no longer implies a static Admin token workflow

Recommended label changes:

- change `Password / Access Token` semantics out of the UI
- label `client_secret` clearly as the Shopify app client secret
- label `shared_secret` clearly as the webhook-signing secret if retained

## `shopify/doctype/shopify_setting/shopify_setting.py`

Required changes:

- validate the new mandatory fields when Shopify is enabled
- stop using `get_password("password")` for webhook registration
- if necessary, call into the auth helper to verify credentials during validation
- preserve existing warehouse and custom-field setup behavior

Optional but recommended:

- add a whitelisted admin action to force token refresh
- add a whitelisted admin action to re-register webhooks

## `shopify/connection.py`

Required changes:

- refactor `temp_shopify_session` to get the token from the auth helper
- refactor `register_webhooks` to use a valid token rather than the legacy settings token field
- refactor `unregister_webhooks` similarly
- keep `_validate_request` behavior intact, but fetch the signing secret from the new secret accessor

Non-negotiable requirement:

- there must be no remaining runtime read of `get_password("password")` in this module once the rewrite is complete

## `shopify/utils.py`

Potential changes:

- if migration helpers or logging helpers assume old credential semantics, update them
- no business rewrite is required here unless credential access is embedded later

## `shopify/tests/*`

Required changes:

- update fixtures and test setup to use `client_id`, `client_secret`, and the new secret fields
- remove assumptions that the settings fixture uses a static `password` token as the active auth path

## Webhook verification design

Webhook validation remains functionally the same:

- compute HMAC-SHA256 over the raw request body
- compare against `X-Shopify-Hmac-Sha256`
- reject unverified payloads before enqueuing handlers

Changes are limited to secret sourcing and secret storage.

Rules:

- always read the signing secret through a dedicated helper
- never hardcode secret-field names outside the auth or webhook helper boundary
- after secret rotation, webhook verification must use the new secret immediately

## Rotation behavior

The module must support regular client-secret rotation.

When `client_secret` changes:

- invalidate the cached access token immediately
- clear `access_token`
- clear `access_token_expires_on`
- set `last_token_refresh_on` only after successful re-acquisition

When the webhook signing secret changes:

- new incoming webhook signatures must be verified against the new secret immediately
- operators must be instructed to verify webhook delivery after the change

Recommended operator actions after secret updates:

- refresh token
- re-register webhooks if needed
- verify one webhook delivery path end to end

## Concurrency considerations

Because bench workers and web requests can run concurrently, the token helper should avoid unnecessary duplicate refreshes.

Minimum acceptable behavior:

- repeated refresh attempts are safe
- the last successful token write wins

Preferred behavior:

- use a short critical section or row-level coordination around token refresh
- re-check token validity after acquiring the lock

The initial implementation may use simpler semantics if correctness is preserved.

## Logging and observability

Auth failures should be visible without exposing secrets.

Log these events:

- token acquisition failure
- token refresh failure
- token invalidation after auth rejection
- webhook validation failure
- webhook re-registration failure

Never log:

- `client_secret`
- `shared_secret`
- raw `access_token`

Acceptable metadata to log:

- shop URL
- handler name
- expiry timestamp
- HTTP status code
- Shopify error payload with secrets removed

## Patch and deployment expectations

This is a flag-day rewrite, so deployment must be coordinated.

Release requirements:

- schema patch for new fields
- runtime code that requires the new credential model
- release notes that state the legacy static token path is unsupported
- upgrade instructions for filling in new credentials before enabling Shopify

Recommended deployment checklist:

1. Deploy schema changes.
2. Populate `client_id`, `client_secret`, and webhook secret fields.
3. Disable or ignore the legacy token field.
4. Run a credential validation step.
5. Trigger webhook re-registration.
6. Trigger a token refresh.
7. Verify one Admin API call and one webhook flow.

## Test plan

## Unit tests

Add tests for:

- token acquisition with valid credentials
- token refresh when missing
- token refresh when expired
- token reuse when still valid
- near-expiry refresh behavior
- cached token invalidation on auth failure
- webhook HMAC validation success
- webhook HMAC validation failure

## Integration tests

Add tests for:

- `temp_shopify_session` using the token helper
- webhook registration using the token helper
- webhook unregistration using the token helper
- behavior when token acquisition fails during settings validation or sync execution

## Regression tests

Protect these existing flows from auth rewrite regressions:

- inbound order sync
- paid order invoice creation
- fulfillment to Delivery Note creation
- cancellation handling
- product upload from ERPNext
- inventory push to Shopify
- historical order backfill

## Documentation deliverables

Update or add documentation for:

- new Shopify setup instructions
- secret-field meaning and ownership
- token lifecycle summary
- credential rotation procedure
- post-upgrade verification checklist

## Acceptance criteria

The rewrite is complete when all of the following are true:

- Shopify Admin API sessions never use the legacy static token field
- `client_id` and `client_secret` are mandatory for enabled Shopify settings
- access tokens are acquired dynamically and refreshed before expiry
- webhook verification still succeeds with the configured signing secret
- token or secret rotation does not require code changes
- order, invoice, fulfillment, product, and inventory flows still work
- tests cover both token lifecycle and webhook verification behavior

## Suggested implementation order

1. Update the `Shopify Setting` schema.
2. Implement `shopify/auth.py`.
3. Refactor `shopify/connection.py` to use the auth helper.
4. Update settings validation behavior.
5. Add token invalidation and rotation behavior.
6. Update tests and fixtures.
7. Update setup and upgrade documentation.

## Open questions

These questions should be resolved before coding starts:

1. Should `shared_secret` remain a separate field, or should webhook verification use `client_secret` directly?
2. Should settings validation actively request a token, or should token acquisition be deferred until first runtime use?
3. Does the Shopify Python library version in this repo work cleanly with short-lived client-credentials tokens in all current API calls?
4. Is a locking strategy needed immediately for token refresh, or can the first implementation accept duplicate refresh attempts?

No implementation should begin until those questions are answered explicitly.