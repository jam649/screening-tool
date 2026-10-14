# Synthetic Business Central contract fixtures

These files are manually authored dummy responses based on verified Microsoft
contracts. They are **not live tenant responses, VCR cassettes, recordings or
customer data**. Fixture tokens and identities have no working authority.

Primary contracts: [Entra client credentials](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow),
[Business Central paths](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/webservices/api-endpoint-structure),
[customer](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_customer),
[vendor](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_vendor),
[paging](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/webservices/server-driven-paging-in-odata-web-services),
[subscriptions](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/dynamics-subscriptions).

Customer and Vendor intentionally share a GUID to test local entity namespaces.
Customer Company/Person supplies type; standard Vendor defaults company.
Posting-blocked records stay eligible for screening. Complete original objects,
street/locality fields and type-aware hash rule v2 are preserved. Phone, blocked
and other raw-only edits retain the last applied party under design §5.4.

Full enumeration uses actual continuation links and Prefer maxpagesize 300,
without a top limit. Timestamp increments overlap by five minutes and preserve
the poll-start cursor. Daily successful full reconciliation is a product policy
for hard deletions, not a claimed vendor timestamp-history limit. Failed pages
never update the book or cursor. Tokens are opaque access tokens with actual
expiry; this application flow has no refresh tokens.
