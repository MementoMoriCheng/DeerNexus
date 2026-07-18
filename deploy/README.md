# DeerNexus Observability Deploy Artifacts (PR-063)

This directory holds Grafana dashboards and Prometheus alerts that consume
the metrics exposed at `/metrics` (see `backend/app/gateway/routers/metrics.py`
and `backend/packages/harness/deerflow/observability/metrics.py`).

All artifacts implement `docs/ops/observability-and-slo.md` §8 (dashboards)
and §9 (alerts). **Only metrics with wired code paths today are covered.**
Deferred panels and alerts (Profile-W HA, Sandbox OOM/quarantine, Redis, Audit
outbox, cost, Policy, OIDC, Console, Backup, cert expiry) are listed as
`TODO` comments in their owning files and land with the PRs that own the
relevant code paths — see `docs/architecture/runtime-contracts.md` §16.26.

## Layout

```
deploy/
├── dashboards/
│   ├── platform-overview.json   # §8.1 — Gateway / Run / SSE / DB / Sandbox
│   ├── runtime.json             # §8.2 — Run state / Model-Tool-MCP / Reconcile / Sandbox
│   ├── control-plane.json       # §8.3 — Auth / DB (Audit outbox TODO until PR-041)
│   └── tenant-ops.json          # §8.4 — platform-wide only (per-Org via Console)
└── alerts/
    └── prometheus-rules.yaml    # §9 — P1/P2 alerts + deferred list in comments
```

## Deploying dashboards (Grafana provisioning)

Drop the JSON files into the Grafana provisioning dashboards directory
(usually `/var/lib/grafana/provisioning/dashboards/`) and configure a
file provider. The dashboards reference a datasource variable
`${DS_PROMETHEUS}` — bind it to your Prometheus datasource in Grafana.

Example `provisioning/dashboards/deernexus.yaml`:

```yaml
apiVersion: 1
providers:
  - name: DeerNexus
    orgId: 1
    folder: DeerNexus
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards/deernexus
```

## Deploying alerts (Prometheus Operator)

```sh
kubectl apply -f deploy/alerts/prometheus-rules.yaml
```

Requires the Prometheus Operator (`monitoring.coreos.com/PrometheusRule` CRD).
For plain Prometheus, translate the `groups:` block into your `rules.yml` and
reload.

## §9 alert annotation contract

Every alert carries all eight fields mandated by §9:
`owner`, `severity`, `summary`, `impact`, `dashboard`, `runbook`,
`silence_rule`, `escalation`. The `dashboard` annotation links to the
Grafana URL path (e.g. `/d/deernexus-platform-overview`); the `runbook`
links to a section in `docs/ops/production-runbook.md`.

§9 also forbids "告警链接要求接收者拥有不必要的跨 Org 权限" — these alerts are
platform-wide only and never reference org-scoped data.
