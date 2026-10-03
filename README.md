# Serverless Events Observability

Every event on an EventBridge bus, indexed into OpenSearch and searchable —
plus a throttled error-alert digest (on by default, and optional), dead-letter
queues and alarms for the paths that can fail silently.

Deploys entirely into your own AWS account. Bring your own OpenSearch domain or
Serverless collection; this stack does not provision one.

```yaml
Resources:
  EventsObservability:
    Type: AWS::Serverless::Application
    Properties:
      Location:
        ApplicationId: arn:aws:serverlessrepo:us-east-1:<account>:applications/AWS-EventBridge-Firehose-OpenSearch-Forwarder
        SemanticVersion: 1.5.0
      Parameters:
        NamePrefix: acme
        EnvironmentName: prod
        OpenSearchEndpoint: search-acme-xxxx.us-east-1.es.amazonaws.com
        OpenSearchResourceArn: arn:aws:es:us-east-1:111122223333:domain/acme/*
        OpenSearchIndex: acme-events
```

That is a complete deployment. Publish to the bus it creates and documents start
appearing in the index.

**The application was renamed.** Up to 1.4.0 it was published as
`serverless-events-observability`, which stays in SAR, frozen at 1.4.0. Every later
version is published as `AWS-EventBridge-Firehose-OpenSearch-Forwarder`. A stack moves
by pointing `ApplicationId` at the new name together with the new `SemanticVersion`;
`NamePrefix` and every resource name are unchanged, so nothing is replaced by the
rename itself.

## Onboarding a new deployment

The quick start above is the whole thing when you own the OpenSearch domain and
want the default topology. This section is the long way round: every decision,
in the order you have to make it, and the two that are hard to reverse.

### Decide these four first

| Decision | Parameter | Hard to change later? |
|---|---|---|
| What the resources are called | `NamePrefix`, `EnvironmentName` | **Yes** — renaming replaces the roles, and a role name may be written into a domain access policy in another account |
| Which index | `OpenSearchIndex` | **Yes** — see below |
| Per-event Lambda, or buffered Firehose | `DeliveryMode` | No — switch and redeploy |
| Whose rule feeds the forwarder | `CreateForwardingRule` | No |

**The index is the one-way door.** An index with no explicit mapping takes each
field's type from the first non-null value it ever sees, and that inference is
permanent. Two deployments sharing an index share that fate: a `"3"` from a test
stage types the field as text for everyone, and every `sum` over it fails from
then on. Either give each deployment its own index, or agree the shape once and
hold it — which is what `ShapingConfig` and the `int` coercer exist to make
possible. Read "The document, and why its shape is configuration" before you
pick.

### 1. Deploy

From the Serverless Application Repository, either nested in a template of your
own (the quick start block) or straight from the console. The minimum is four
parameters:

```
NamePrefix             acme            # every resource is ${NamePrefix}-...-${EnvironmentName}
EnvironmentName        prod
OpenSearchEndpoint     search-acme-xxxx.us-east-1.es.amazonaws.com
OpenSearchResourceArn  arn:aws:es:us-east-1:111122223333:domain/acme/*
```

`OpenSearchIndex` defaults to `${NamePrefix}-events`. Everything else has a
working default.

If your domain is in a **different account** from this stack, stop after this
deploy and do step 2 before anything else.

### 2. Grant the stack's role on the domain

The stack signs its writes with SigV4 as its own execution role. A cross-account
domain will not accept that role until its access policy names it.

Take the role ARN from the stack outputs:

| Output | When |
|---|---|
| `ForwarderRoleArn` | `DeliveryMode=Lambda` |
| `FirehoseDeliveryRoleArn` | `DeliveryMode=Firehose` |

and add it to the domain's access policy. Two things about doing this that cost
real time to learn:

- **A domain access policy naming a principal that does not resolve is rejected
  outright** — `409 InvalidTypeException`, roughly **31 minutes** after the
  update starts. Not at submit time. So deploy the stack first and grant second,
  never the reverse, and when you remove a deployment, remove the grant *before*
  the role is deleted. The rule runs in both directions and the feedback loop is
  half an hour long either way.
- **Firehose needs read as well as write.** `CreateDeliveryStream` probes the
  cluster before it will create the stream, and without the read half it fails
  with a message about the endpoint being unreachable, which is not what is
  wrong. See "Firehose against a cross-account domain takes two deploys".

### 3. Point events at it

Nothing is forwarded until a rule feeds the forwarder.

- **`CreateForwardingRule=true`** (the default) — the stack creates a rule
  matching every event in its own account on the bus, and the permission that
  lets the rule invoke the function. Nothing else to do.
- **`CreateForwardingRule=false`** — you own the rule. Point it at the
  `ForwarderFunctionArn` or `DeliveryStreamArn` output. Choose this when the
  filtering is yours, or when a rule already exists and has to keep its name.

Filtering at the rule is also the cheapest place to filter: an event that never
matches is never delivered and never billed.

> **Do not point your own rule at the forwarder *and* leave
> `CreateForwardingRule=true`.** Both rules fire, every event is indexed twice,
> and the duplicates have distinct ids so nothing dedupes them.

If you point an existing rule at the function yourself, add the invoke
permission yourself too — `CreateForwardingRule=false` does not create one,
because it does not know which rule you mean.

### 4. Shape the document

`ShapingConfig` is a JSON field map, not code. The default lifts the conventional
envelope fields and keeps the whole detail as `payload`. Override it when your
events call things something else, when a field should be promoted to the top
level so it can be aggregated, or when a fallback should differ:

```json
{"defaults": {"application": "acme"},
 "promote": [{"from": "num_pages", "as": "num_pages", "type": "int"}]}
```

An invalid config fails at cold start rather than silently shaping differently.
That is deliberate: a shape change decides an index mapping permanently, so it
belongs in a reviewed deploy, not in a value someone can edit at runtime.

### 5. Verify before you believe it

A stack that reaches `CREATE_COMPLETE` has proved nothing about delivery. Publish
a marked event and read it back out of the index:

```bash
aws events put-events --entries '[{
  "Source":"acme.verify","DetailType":"Probe",
  "EventBusName":"<your bus>",
  "Detail":"{\"organization\":\"probe-001\",\"username\":\"validator\"}"}]'
```

then query the index for `probe-001`. Under Lambda delivery it is near-immediate;
under Firehose allow about **2× `FirehoseBufferIntervalSeconds`**, because the
processing and delivery buffers are in series.

Worth also proving, once, while you still remember what you configured:

| Probe | Expect |
|---|---|
| Publish the same event twice | **two** documents — re-ingestion is not deduped |
| A field you promoted as `int`, sent as `"7"` | lands as `7` |
| The same field sent as `"seven"` | lands as null, and the document still indexes |
| An event your rule should not match | never arrives |

### 6. Subscribe to the alarms

The stack creates alarms and an SNS topic, and a topic with no subscription
fails silently and convincingly — `describe-alarm-history` will report the action
"successfully executed" into nothing. Set `AlertEmail`, then **confirm the
subscription**; it sits in `PendingConfirmation` until someone clicks the link,
and an unconfirmed subscription is the same as no subscription.

If you do not want the alerting at all, set `CreateErrorAlerting=false` instead
and skip this step — see "Leaving the error digest out".

## The document, and why its shape is configuration

Each event becomes one document:

```json
{
  "event_id":    "b4f1…",
  "source":      "billing.invoice",
  "event":       "InvoiceIssued",
  "environment": "prod",
  "application": "billing",
  "organization": "acme-corp",
  "username":    "jdoe",
  "timestamp":   "2026-09-17T09:12:44.518Z",
  "payload":     "{\"invoice_id\":\"INV-9\",\"total\":1200}"
}
```

`payload` is the entire event detail, stored as a **JSON string**. That is
deliberate: a per-event detail shape cannot be mapped into a rigid index mapping
without eventually colliding. The cost is that nothing inside `payload` can be
aggregated or filtered on — so anything you want to query lives at the top level.

Which is what `ShapingConfig` is for:

```json
{
  "defaults": {"application": "billing"},
  "promote":  [{"from": "total", "as": "invoice_total", "type": "int"}],
  "nested":   {"actor": {"user": "username", "org": "organization"}}
}
```

- **`defaults`** — fallbacks for the four core fields (`environment`,
  `application`, `organization`, `username`) when an event omits them or sends an
  empty value.
- **`promote`** — lift a top-level detail key out into its own document field so
  it can be summed, filtered and charted. `type` is `int`, `str` or `raw`.
- **`nested`** — build a composite object out of resolved core fields.

Anything the config cannot honour exactly fails the cold start with a named
error. It never falls back to a different shape — see below for why that matters
more than it sounds.

### Choose `type` deliberately

An index with no explicit mapping takes each field's type from **the first
non-null value it ever sees**, permanently. An emitter that sends `"12"` where it
once sent `12` maps the field as text, and every aggregation over it fails from
then on — fixable only by a reindex. Worse, indexing errors on the per-event path
are swallowed, so a mapping conflict discards the **whole document** and raises
nothing.

`"type": "int"` coerces what is unambiguously an integer and drops everything
else — including booleans, which are integers in Python and never a real count.
The coercers exist so that a careless emitter cannot decide your mapping.

## Delivery modes

`DeliveryMode=Lambda` (default) invokes the forwarder once per event, which
indexes one document per invocation. Simple, and fine at low volume.

`DeliveryMode=Firehose` points the rule at an Amazon Data Firehose stream.
Firehose buffers records, calls the same function as a *transformation* (a batch
in, shaped documents out), then bulk-indexes — and owns retry plus backup of
rejected documents to S3. At volume this is the difference between N HTTP round
trips and one `_bulk`.

Firehose also fixes a real blind spot: a document OpenSearch rejects lands in
`failed/<env>/` in the backup bucket and moves
`DeliveryToAmazonOpenSearchService.Success`, which Firehose measures rather than
this code — so it cannot be swallowed the way the per-event path swallows it.

### Capabilities — and they differ by how you deploy

This template creates IAM roles with **explicit names** and attaches **resource
policies** (SQS queue policies, an SNS topic policy). Both need acknowledging,
and the acknowledgement is spelled differently depending on which API you go
through. Getting it wrong fails the deploy before anything is created.

**Deploying from the Serverless Application Repository** — `serverlessrepo`
requires `CAPABILITY_RESOURCE_POLICY` and refuses without it:

```bash
aws serverlessrepo create-cloud-formation-change-set \
  --application-id <application arn> --semantic-version <version> \
  --stack-name <your stack name> \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
                 CAPABILITY_AUTO_EXPAND CAPABILITY_RESOURCE_POLICY \
  --parameter-overrides file://params.json
```

Omit it and you get, before any resource exists:

```
Required capabilities [CAPABILITY_RESOURCE_POLICY] were not provided.
```

**Deploying or updating through CloudFormation directly** — `create-change-set`
and `sam deploy` **reject** `CAPABILITY_RESOURCE_POLICY`; their enum does not
contain it:

```bash
aws cloudformation create-change-set \
  --stack-name <your stack name> --template-url <url> \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameters file://params.json
```

Pass it anyway and you get:

```
Value '[…, CAPABILITY_RESOURCE_POLICY]' at 'capabilities' failed to satisfy
constraint: Member must satisfy enum value set:
[CAPABILITY_AUTO_EXPAND, CAPABILITY_NAMED_IAM, CAPABILITY_IAM]
```

So the two paths are not interchangeable, and a tenant migrating an existing
stack will use both: `serverlessrepo` for a fresh deploy, CloudFormation for an
in-place update of a stack that already exists.

**`CAPABILITY_NAMED_IAM`, not `CAPABILITY_IAM`.** The Firehose delivery role has
a fixed name on purpose — its ARN is written into an OpenSearch access policy,
possibly in another account, and a CloudFormation-generated name would silently
invalidate that grant every time the role is replaced.

### Which region to deploy in

The application is published in **us-east-1 and us-west-1**, and the two are
separate listings with separate ARNs. Deploy from the one matching the region
your stack lives in.

This is not a convenience. A SAR application exists only in the region it was
published to, and Lambda requires its code bucket to be in the **same region as
the function**. Minting a template from another region hands you a `CodeUri`
pointing at a bucket that Lambda cannot read, and the stack fails with:

```
S3 Error Code: PermanentRedirect. The bucket you are attempting to access must
be addressed using the specified endpoint.
```

Your stack has to sit in the same region as the EventBridge bus it listens to,
because a rule cannot invoke a Lambda in another region. The OpenSearch domain
may be anywhere — set `OpenSearchRegion` and the forwarder signs for it. Under
`DeliveryMode=Firehose` the stream must be in the domain's region, which is the
one case where the bus and the delivery path can end up split across two stacks.

### If a Region's concurrency limit is small

The alert digest reserves 1 concurrent execution, which is what caps alerts at
about one email per minute. AWS refuses a reservation that would drop the
account's unreserved concurrency below 10 — and an **unactivated Region defaults
to a limit of 10 in total**, so the reservation fails and the stack rolls back:

```
Specified ReservedConcurrentExecutions for function decreases account's
UnreservedConcurrentExecution below its minimum value of [10]
```

Set `DigestReservedConcurrency=0` to reserve none. The digest then shares the
account pool, and the one-email-per-minute guarantee becomes a tendency rather
than a cap. Better: raise the Region's limit and leave it at 1.

Worth checking before you pick a Region — `aws lambda get-account-settings`
in the one you intend to deploy to. A limit of 10 usually means nothing has ever
run there, which is rarely where you want an event pipeline.

### Leaving the error digest out

`CreateErrorAlerting` decides whether the stack builds the error-alert digest at
all. It defaults to `true`, and at `true` the stack is exactly what earlier
versions deployed, so an existing deployment upgrades with no resource changes.

Set it `false` when errors are already read somewhere else — typically in
OpenSearch itself, where the forwarder indexes `Error`, `ProcessingError` and
`IntegrationError` events like every other event — and a digest email would
only be a second, noisier copy. The stack then creates none of:

| Resource | What it was for |
|---|---|
| `ErrorAlertsRule` | Matches the three error detail-types on the bus |
| `ErrorAlertsQueue`, `ErrorAlertsDLQ`, `ErrorAlertsQueuePolicy` | Buffers them for the digest |
| `AlertDigestFunction`, `AlertDigestLogGroup` | Batches them into one notification a minute |
| `AlertsSnsTopic`, `AlertsSnsTopicPolicy`, `AlertsEmailSubscription` | Delivers the notification |
| `ErrorAlertsDlqDepthAlarm` | Watches the digest's own dead-letter queue |

and the `AlertsSnsTopicArn` and `ErrorAlertsQueueArn` outputs go with them.
`AlertEmail` and `DigestReservedConcurrency` are ignored, since there is no
topic to subscribe to and no digest to reserve for.

Two things it does **not** do:

- **It does not stop error events being indexed.** Only the digest goes. The
  forwarder never filtered on detail-type, so errors land in OpenSearch either
  way.
- **It does not remove the other alarms.** The bus, forwarder and Firehose
  alarms watch the delivery path, not your events, and stay — but with no
  actions, because the topic they notified is gone. They still change state in
  CloudWatch, and every state change is still published to the account's
  default bus as a `CloudWatch Alarm State Change` event if you want to route one
  yourself.

Switching an existing stack to `false` deletes the topic, so anything subscribed
to it outside this stack stops receiving. Switching back to `true` creates a
new topic with the same name, and its subscriptions start from nothing — an
email subscription needs confirming again.

### Pinning the forwarder's role name

`ForwarderRoleName` fixes the execution role's name instead of letting
CloudFormation generate one. Leave it blank unless you need it.

You need it when the role's ARN is written into an access policy **somewhere
else**. A cross-account OpenSearch domain names its principals exactly, so a
generated name means every replacement of the function silently invalidates the
grant — and the symptom is every document 403ing with no code change to blame.
The same reasoning already applies to the Firehose delivery role, which is named
unconditionally for exactly this reason.

It is also how an existing deployment moves onto this product without touching
the grant: reuse the name the old stack used, and the principal never changes.
Two stacks cannot hold one role name at the same time, so the old stack has to
release it first — which makes the cutover order matter.

## Firehose against a cross-account domain takes two deploys

Firehose verifies at `CreateDeliveryStream` time that its delivery role can reach
the cluster. But that role is created by this stack, and OpenSearch will not
accept a principal ARN that does not exist yet. One deploy cannot do both.

1. Deploy with `FirehoseStreamEnabled=false`. Creates the role, the bucket and
   the log group, and nothing that talks to OpenSearch.
2. Take `FirehoseDeliveryRoleArn` from the outputs and add it to the domain's
   access policy.
3. Deploy again with `FirehoseStreamEnabled=true`. A successful
   `CreateDeliveryStream` **is** the proof that step 2 worked.

Two things about that grant, both of which cost someone a day to find:

- **Firehose signs as its own delivery role, not the forwarder's execution
  role.** Granting the Lambda role is not enough.
- **The read half is not optional, and it needs the bare domain root** —
  `…:domain/name` with no trailing `/*`, which the `/*` wildcard does not match —
  plus `_all/_settings`, `_cluster/stats`, `_nodes*`, `_stats` and
  `<index>*/_mapping`. Firehose probes the cluster before delivering. Omitting
  the root is what produces the misleading *"Verify that the IAM role has access
  to the Elasticsearch cluster endpoint"*.

`ClusterEndpoint` is used as given and must resolve in **your** account — a
cross-account domain cannot be addressed by `DomainARN`.

### Latency under Firehose is about 2× the buffer interval

`FirehoseBufferIntervalSeconds` applies to **both** of the stream's buffers,
which sit in series: the processing buffer before the transform and the delivery
buffer before the bulk write. Measured end to end: 105s at 60, 41.8s at 30. Set
it with that doubling in mind if anything reads the index interactively.

## The search API

Off by default. `CreateSearchApi=true` adds an HTTP API over the same index the
forwarder writes to:

```
GET /search        filter by source, detail-type, organization, free text, time range
GET /facets        the distinct (source, detail-type) pairs present
GET /{event_id}    one archived event
GET /openapi.json  the generated spec
```

Results are newest-first and paged (`size`, max 200; `offset`). `payload` comes
back as an object, not the JSON string it is stored as — the string exists so
OpenSearch never has to map a per-event shape, and undoing that on the way out is
this API's job rather than every caller's.

Filters match exactly, on `.keyword` sub-fields. An analysed match would return
`billing.invoice` for `source=billing`, which is not what a filter means. `q` is
the one fuzzy parameter and searches the payload text.

**The API is unauthenticated unless you set `SearchApiAuthorizerArn`.** Point it
at a Lambda authorizer — identity from the `Authorization` header, simple
responses — and every route but `/openapi.json` goes behind it. The archive holds
every event on your bus, which is usually more than you want world-readable.

No custom domain is created. Take `SearchApiEndpoint` or `SearchApiId` and map
your own; DNS is yours, and a stack that forwards events should not own it.

`POST /emit` publishes to the bus on the caller's behalf, for clients that cannot
reach EventBridge directly. It is behind `EnableEmitEndpoint=true` and off by
default because it is the only route that writes.

The API's role is read-only on the index. It cannot write the archive it serves —
`/emit` goes through the bus so the forwarder still decides what gets indexed.

## Bringing your own bus, or your own rule

Both are parameters, because in a mature account neither is usually yours to
create:

- **`EventBusName`** — blank creates a bus named
  `<NamePrefix>-events-<EnvironmentName>`; set it and the stack attaches to an
  existing bus and creates nothing. Two IaC tools owning one bus is how a
  rollback destroys a resource the other tool believes it manages.
- **`CreateForwardingRule`** — `true` creates a rule matching every event in the
  account. Set it `false` when the filtering is yours — a source allowlist, a
  detail-type filter, or a rule that already exists and must keep its name — and
  point your own rule at the `ForwarderFunctionArn` or `DeliveryStreamArn`
  output. Filtering at the rule is also the cheapest place to filter: an event
  that never matches is never delivered and never billed.

  A Firehose target needs `EventsToFirehoseRoleArn` as the target's `RoleArn` —
  unlike a Lambda target, there is no resource policy to attach on the Firehose
  side.

  Set it `false` and forget to create a rule and nothing is forwarded. Set it
  `true` alongside an existing rule and every event is indexed twice.

## OpenSearch Serverless

Set `OpenSearchServiceName=aoss` and point `OpenSearchResourceArn` at the
collection. Add `ForwarderRoleArn` to a data access policy on the collection.

Firehose mode requires `es` — Firehose reaches a Serverless collection through a
different destination block, which is not wired up here. Use Lambda mode for
`aoss`.

## Verifying a deployment

The surface is **EventBridge in, an OpenSearch document out**. Not the Lambda,
not the template. Publish to the bus, then read the index.

Under Firehose, the reconciliation invariant is:

```
IncomingRecords == DeliveryToAmazonOpenSearchService.Records
                 + <objects under failed/<env>/ in the backup bucket>
                 + <delivery DLQ depth>
```

`IncomingRecords` is published before the delivered-records metric for the same
batch, so a check run immediately after publishing shows a transient gap of
exactly the batch size. Poll until they converge before calling it a leak.

Worth probing explicitly, because they are the easy things to get wrong: an event
whose source your rule does not match, an event missing a field your config
promotes, and the same event published twice (you get two documents — re-ingestion
is not deduplicated).

## Alarms

On the bus: `FailedInvocations`, `ThrottledRules`. On the forwarder: errors,
duration, and an invocation spike. On the error path: DLQ depth. Under Firehose:
`DeliveryToAmazonOpenSearchService.Success` **and** `.DataFreshness` — a stalled
stream reports no failures at all, so `Success` alone would never fire.

All of them publish to the `AlertsSnsTopicArn` output. **Subscribe something to
it.** An SNS topic with no subscriptions accepts every publish and reports
success, and `describe-alarm-history` will say "Successfully executed action"
while nobody is told anything.

Under `CreateErrorAlerting=false` there is no topic: the DLQ-depth alarm goes
with the queue it watched, and the rest are created with no actions.

## Developing

This repository is the product's source: changes land here, by pull request.

| Path | What it is |
|---|---|
| `app/` | The Lambda code. The only thing packaged: every function has `CodeUri: app/` |
| `template.yaml` | The SAM template, and the SAR metadata, including `SemanticVersion` |
| `tests/` | Unit tests. `tests/fixtures/shaping/` holds example `ShapingConfig` maps |
| `scripts/` | Gates: the leak check, the SAR metadata check, the built-artifact check |
| `Makefile` | Every command below. `make help` lists them |

It holds no deployment's configuration. A deployment's `ShapingConfig`, stack
parameters and runbooks belong in the repository that deploys it, next to the
release it pins (see "Pinning a release").

### Tests run in two lanes

The difference is the installed packages, not the command:

```bash
# Lane 1, the seam: the delivery dependencies must be ABSENT. This is what proves
# shaping.py imports without opensearch-py, boto3 or requests-aws4auth; the
# search API tests skip here.
python3.13 -m venv /tmp/eo-seam && . /tmp/eo-seam/bin/activate
pip install -r tests/requirements.txt
make test

# Lane 2, everything the Lambda has, so the search API is covered.
python3.13 -m venv /tmp/eo-full && . /tmp/eo-full/bin/activate
pip install -r tests/requirements-full.txt
make check        # sam validate, the full lane, the leak check, the SAR metadata check
```

Use Python 3.13, which is the Lambda runtime in `template.yaml`. pydantic-core is
compiled per Python version, so a test run on another version tests something
else.

### The leak gate

`make leak-check` scans the **whole** tree, not a published subset: in a public
repository nothing is private. It refuses any 12-digit number that is not an AWS
documentation placeholder, any email address outside the reserved example
domains, credentials, and host CIDRs. Organisation-specific terms can be added
without committing them: one per line in a git-ignored `.leak-check-deny` file,
or comma-separated in the `LEAK_CHECK_DENY` environment variable (a CI secret).
Test fixtures that must contain a forbidden shape assemble it at runtime; see
`tests/test_leak_check.py`.

### CI

`.github/workflows/ci.yml` runs on every pull request and on every push to
`main`: the leak gate, both test lanes, the SAR metadata check and
`sam validate --lint`. It needs no AWS credentials and publishes nothing.

`.github/workflows/release.yml` (**Create Release**, run by hand) tags `main`
and creates the GitHub release, then starts `.github/workflows/publish.yml`
(**Publish to SAR**) on that tag; see below.

## Releasing

A release is a git tag, `vX.Y.Z`, equal to the `SemanticVersion` in
`template.yaml`. Version numbers come from the SAR listing: tag `v0.0.1` predates
this repository being the source, and is not a release of this code.

1. In the pull request, bump `SemanticVersion` in `template.yaml` and the version
   in the quick start above. `make check-metadata` fails if they disagree. A
   published version is immutable, so a version that has ever been published
   cannot be reused.
2. Merge to `main`.
3. Run **Create Release** (Actions → Create Release → Run workflow, on `main`).
   It reads `SemanticVersion` from `template.yaml`, refuses if `vX.Y.Z` already
   exists, runs the CI gates on that exact commit, then creates the tag and the
   GitHub release. There is no version input: the number is the one in the
   template, so the tag and the SAR version cannot disagree.
4. Publishing follows on its own: Create Release dispatches **Publish to SAR**
   on the new tag (so does pushing a `v*` tag by hand). It reruns the gates on
   the tag, builds in a container, checks the built artifact carries its
   dependencies, packages per region, leak-checks the packaged templates and
   publishes to every region the application is listed in, skipping any region
   that already has the version. To retry, run it again with the tag selected
   under "Use workflow from".

   AWS access is GitHub OIDC into a role that can only add versions to this
   application. The role trusts the `sar-publish` environment, which admits
   tags `v*` only, and the environment holds the role ARN and the artifact
   bucket names as secrets. The publishing account id is masked in the logs.

   Publishing from your own fork or account instead: from a checkout of the
   tag, `make release S3_BUCKET=<artifact bucket in that region> REGION=<region>`,
   once per region.

## Pinning a release

Anything that builds or deploys this product pins a release; nothing tracks
`main`.

- **Deploying from SAR** — pin `SemanticVersion`, as in the quick start.
- **Building from source** — pin a tag `vX.Y.Z`, or a full 40-character commit
  SHA. A tag can be moved and a SHA cannot, so pin the SHA when the build must be
  reproducible and record the tag beside it:

  ```yaml
  - uses: actions/checkout@v4
    with:
      repository: jnet-platform-factory/aws-eventbridge-firehose-opensearch-forwarder
      ref: <full commit sha>   # vX.Y.Z
      path: events-observability
  ```

Upgrading is then a reviewed change to that one pin in the deploying repository.

## Licence

Apache-2.0.
