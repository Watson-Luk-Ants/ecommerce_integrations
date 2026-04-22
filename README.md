<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Ecommerce Integrations for ERPNext</h2>

[![CI](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml)

</div>

### Currently supported integrations:

- Shopify - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/shopify_integration)
- Unicommerce - [User Documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration)
- Zenoti - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration)
- Amazon - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration)

### Installation

- Frappe Cloud Users can install [from Marketplace](https://frappecloud.com/marketplace/apps/ecommerce_integrations).
- Self Hosted users can install using Bench:

```bash
# Production installation
$ bench get-app ecommerce_integrations --branch main

# OR development install
$ bench get-app ecommerce_integrations  --branch develop

# install on site
$ bench --site sitename install-app ecommerce_integrations
```

After installation follow user documentation for each integration to set it up.

### Shopify connector notes

- Shopify authentication uses `Client ID` and `Client Secret`. The connector obtains an Admin API access token through Shopify's [client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant) — no authorization code or OAuth redirect is needed. The app must be installed on the shop first (otherwise Shopify returns `app_not_installed`).
- Client credentials tokens are short-lived (~24h); the connector stores the expiry and automatically refreshes the token before it expires, so background sync keeps working without manual intervention.
- Shopify connection testing and webhook synchronization now use the GraphQL Admin API. Order, product, inventory, and import synchronization continue to use the existing REST compatibility layer.
- Shopify webhooks are no longer destructively deleted and recreated during every settings save. Use the explicit webhook sync action when credentials or callback endpoints change.

Example placeholders for `Shopify Setting`:

```text
Shop URL: your-store.myshopify.com
Client ID: <SHOPIFY_CLIENT_ID>
Client Secret: <SHOPIFY_CLIENT_SECRET>
Admin API Version: 2026-01
```

Manual test flow:

1. Create the app in the Shopify Dev Dashboard and **install it on the shop** (the client credentials grant fails with `app_not_installed` otherwise).
2. Open `Shopify Setting`, paste your own values for `Client ID` and `Client Secret` using the placeholders above as a guide, then save.
3. Click `Acquire Access Token` to fetch and store the Shopify Admin API token via the client credentials grant.
4. Click `Test Connection` to verify GraphQL Admin API access.
5. Click `Sync Shopify Webhooks` to register or update the connector-owned webhook subscriptions.

### Contributing

- Follow general [ERPNext contribution guideline](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)
- Send PRs to `develop` branch only.

### Development setup

- Enable developer mode.
- If you want to use a tunnel for local development. Set `localtunnel_url` parameter in your site_config file with ngrok / localtunnel URL. This will be used in most places to register webhooks. Likewise, use this parameter wherever you're sending current site URL to integrations in development mode.

#### License

GNU GPL v3.0
