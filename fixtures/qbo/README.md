# Synthetic QuickBooks contract fixtures

These responses are manually authored dummy data derived from verified official
Intuit contracts. They are **not live recordings, VCR cassettes, sandbox evidence,
or customer records**. Dummy tokens are fixtures, never working credentials.

Contracts: [OAuth/token/revocation](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/oauth-2.0.html),
[full queries](https://static.developer.intuit.com/output_html/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries.html),
[CDC](https://static.developer.intuit.com/output_html/qbo/docs/learn/explore-the-quickbooks-online-api/change-data-capture.html).

Customer and Vendor share published IDs intentionally; local IDs include the entity
namespace. CompanyName is primary for companies. Otherwise explicit given/family
names indicate an individual; DisplayName alone defaults conservatively to a
company. DisplayName and PrintOnCheckName are aliases. Hierarchical names and
individual contact names of companies stay in the unmodified raw record.
BillAddr supplies primary locality; ShipAddr is the fallback. Both street-line
sets and the full raw vendor object survive normalization. Inactive/deleted rows
are identity-only tombstones, never replacement empty-name parties.

Full queries explicitly enumerate Active true and false with numeric pagination.
CDC uses five-minute overlap and the poll-start watermark. Thirty-day gaps,
1,000-object saturation, or unverifiable completeness trigger a full snapshot.
Malformed or failed pages never reconcile the book or advance its cursor. Actual
rotating refresh tokens persist before later data requests; unchanged matching
hashes retain the original raw party record under the v1 no-write contract.
