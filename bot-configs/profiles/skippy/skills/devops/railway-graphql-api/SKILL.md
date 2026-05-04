---
name: railway-graphql-api
description: Use Railway's authenticated GraphQL API for operations the CLI doesn't expose — e.g. swapping a service's GitHub source repo. The `railway` CLI stores an OAuth access token in ~/.railway/config.json that authenticates against backboard.railway.com/graphql/v2.
version: 1.0.0
---

# Railway GraphQL API

Railway's CLI is a thin client over a GraphQL backend at `https://backboard.railway.com/graphql/v2`. Many ops the CLI hides (changing the GitHub source repo, fetching trigger details, mutating service config) are one mutation away.

## Auth

Token is stored at `~/.railway/config.json` under `user.accessToken` (refreshed by `railway login`):

```bash
TOKEN=$(python3 -c "import json; print(json.load(open('/Users/wondermonkey/.railway/config.json'))['user']['accessToken'])")
```

Send as `Authorization: Bearer $TOKEN`.

## Find service / trigger IDs

`railway link` (interactive) connects you to a project, then:

```bash
railway status --json | python3 -m json.tool
```

Gives you `serviceId`. Or query directly:

```bash
curl -s -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"query($id: String!){ service(id:$id){ id name repoTriggers{ edges{ node{ id repository branch } } } } }","variables":{"id":"<SERVICE_ID>"}}'
```

## Swap GitHub source repo

The trigger is a `DeploymentTrigger` (CLI calls it `repoTrigger` in some places — they're the same node).

```bash
TRIGGER_ID="..."
curl -s -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"mutation($id: String!, $input: DeploymentTriggerUpdateInput!){ deploymentTriggerUpdate(id:$id, input:$input){ id repository branch } }","variables":{"id":"'"$TRIGGER_ID"'","input":{"repository":"owner/new-repo","branch":"main"}}}'
```

The new repo must already be connected to the same Railway-GitHub install. If it isn't, the API will accept the mutation but deploys will silently 403 against GitHub.

## Verify

Re-query the service. Then `railway redeploy -y` to test, or push a commit to the new repo and watch `railway logs`.

## Other useful queries

- `me { name email }` — sanity check auth
- `project(id: ...) { name services { edges { node { id name } } } }`
- `service(id: ...) { latestDeployment { id status url } }`
- Schema introspection: `{ __schema { types { name } } }` then narrow with `__type(name:"DeploymentTriggerUpdateInput") { inputFields { name type { name } } }`

## Pitfall

GraphQL field names changed over time. If a mutation/field 404s, check the error message — Railway's server suggests the closest valid name (e.g. `repoTriggerUpdate` → `deploymentTriggerUpdate`).
