# Doppler writer contract questions

Historical draft only; not submitted. Superseded by the user's basic-sync design decision; these answers are no longer a release prerequisite. No account identifiers, tokens or secret values are included.

Our runtime coordinates writers using Git ownership, but a request can remain in flight after an ownership transfer. Before enabling shared Doppler writes, we need documented provider-side guarantees for the following cases. Please supply authoritative documentation or written confirmation and the relevant endpoint, field/header, limits and error semantics.

1. **Mutation fencing:** Can a config mutation carry a lease or monotonically increasing fencing token that Doppler checks at commit time? If writer A submits a request, writer B takes ownership and invalidates A, can A's already-submitted request still commit afterward? If credential revocation is the intended mechanism, what guarantees cover requests already accepted or executing?
2. **Conditional update and merge:** Is there an atomic expected-old-value or config-revision precondition for updating selected keys? What happens when the precondition is stale? When two writers change different keys in one config concurrently, are both updates preserved and are omitted keys guaranteed unchanged? Please distinguish partial `change_requests` behavior from concurrency guarantees.
3. **Terminal operation evidence:** Can the client supply a durable operation/idempotency identifier and query its accepted, rejected, pending or terminal status after a timeout? What are retention, duplicate-request and retry semantics? Can a returned activity/event/version identifier be correlated unambiguously with that client operation, and does its status prove that a delayed request can no longer commit?

Current public evidence: [Update API](https://docs.doppler.com/reference/secrets-update) and [official CLI SetSecrets implementation](https://raw.githubusercontent.com/DopplerHQ/cli/master/pkg/http/api.go). The observed CLI path does not supply those safety fields. This is not a claim that no other Doppler facility exists.

Until these properties are established, the runtime keeps the production writer disabled. Testing a disposable config would characterize observations but would not establish guarantees for a delayed stale write. An unavailable property requires an explicit product/design decision; it must not be silently replaced with read-before-write polling.
