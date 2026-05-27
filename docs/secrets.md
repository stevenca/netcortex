# Secrets Management

NetCortex uses an external secret backend for **all** sensitive values — no secrets in NetBox, no plaintext in config files. NetBox remains the data backend (inventory, topology, documents) but holds no credentials.

---

## Design

### Two-Phase Bootstrap

```
Phase 1 — Bootstrap (env only)
  └─ SECRET_BACKEND=aws_sm | vault
  └─ Backend auth (IAM role / Vault token / AppRole)
  └─ NC_SECRET_PREFIX=netcortex     ← path namespace

Phase 2 — Hydrate (from secret backend)
  └─ netcortex/core                       ← NetBox URL/token, MCP secret, Redis URL, ...
  └─ netcortex/adapters/_index            ← List of all adapter instances
  └─ netcortex/adapters/{type}/{name}     ← Per-instance API keys, URLs, passwords
  └─ netcortex/devices/site/{slug}        ← Site-wide device credentials
  └─ netcortex/devices/host/{hostname}    ← Per-device credential overrides
```

Phase 1 env vars are the only values that must be injected at the Docker/ECS/K8s level. Everything else flows through the backend.

---

## Secret Path Schema

All paths are relative to the configured prefix (default: `netcortex`).

### Core and Adapter Index

| Full path | Contents | Required |
|---|---|---|
| `netcortex/core` | `netbox_url`, `netbox_token`, `mcp_secret`, `redis_url`, + optional tuning | ✅ |
| `netcortex/adapters/_index` | JSON list of all adapter instances | ✅ if using adapters |

### Adapter Instance Credentials

Each adapter instance has its own secret: `netcortex/adapters/{type}/{instance_name}`.
Multiple instances of the same type are fully supported — one secret per instance.

| Example path | Contents |
|---|---|
| `netcortex/adapters/meraki/corp` | `api_key`, `org_id`, `base_url` (optional — for non-standard or gov endpoints) |
| `netcortex/adapters/meraki/branch` | `api_key`, `org_id` (different org and API key) |
| `netcortex/adapters/catalyst_center/dc1` | `url`, `username`, `password` |
| `netcortex/adapters/catalyst_center/dc2` | `url`, `username`, `password` |
| `netcortex/adapters/intersight/primary` | `key_id`, `secret_key`, `base_url` (optional) |
| `netcortex/adapters/nexus_dashboard/dc1-prod` | `url`, `username`, `password` |
| `netcortex/adapters/nexus_dashboard/dc2-prod` | `url`, `username`, `password` |
| `netcortex/adapters/fmc/onprem-prod` | `deployment_mode=onprem`, `url`, `username`, `password`, `domain_uuid` (optional; auto-discovered if omitted), `domain_name` (optional selector), `expand_details` (default true) |
| `netcortex/adapters/fmc/cloud-prod` | `deployment_mode=cdfmc`, `key_id`, `access_token`, `refresh_token`, `domain_uuid` (optional; auto-discovered if omitted), `domain_name` (optional selector), `region` or `base_url`, `expand_details` (default true) |
| `netcortex/adapters/snmp/legacy-floor2` | `community`, `version`, `ip_range`, `auth_key`, `priv_key` |

### Device Credentials

| Full path | Contents | Notes |
|---|---|---|
| `netcortex/devices/site/{site_slug}` | `ssh_username`, `ssh_password`, `ssh_enable_secret`, `netconf_username`, `netconf_password` | Site-wide defaults |
| `netcortex/devices/host/{hostname}` | Same keys — overrides site defaults | Per-device override |

### `netcortex/adapters/_index` schema

This single secret declares every adapter instance. NetCortex reads it at startup to know what to load.

```json
{
  "instances": [
    {"type": "meraki",           "name": "corp",         "enabled": true},
    {"type": "meraki",           "name": "branch",       "enabled": true},
    {"type": "catalyst_center",  "name": "dc1",          "enabled": true},
    {"type": "catalyst_center",  "name": "dc2",          "enabled": true},
    {"type": "intersight",       "name": "primary",      "enabled": true},
    {"type": "nexus_dashboard",  "name": "dc1-prod",     "enabled": true},
    {"type": "nexus_dashboard",  "name": "dc2-prod",     "enabled": false},
    {"type": "fmc",              "name": "onprem-prod",  "enabled": true},
    {"type": "fmc",              "name": "cloud-prod",   "enabled": false},
    {"type": "snmp",             "name": "legacy-floor2","enabled": true}
  ]
}
```

`enabled: false` skips an instance at startup without deleting its secret — useful for temporarily disabling a platform.

The **instance ID** used throughout NetCortex (MCP tools, sync status, logs, NetBox `nc_platform` field) is always `"{type}/{name}"`, e.g. `meraki/corp`, `catalyst_center/dc1`.

### `netcortex/core` full schema

```json
{
  "netbox_url": "https://netbox.example.com",
  "netbox_token": "your-netbox-api-token",
  "mcp_secret": "random-32-byte-hex-secret",
  "redis_url": "redis://redis:6379/0",
  "log_level": "INFO",
  "log_format": "json",
  "mcp_transport": "http",
  "sync_backend": "apscheduler",
  "sync_conflict_policy": "alert",
  "sync_interval_meraki": 3600,
  "sync_interval_catalyst_center": 600,
  "sync_interval_intersight": 3600,
  "sync_interval_snmp": 1800,
  "ssh_timeout": 30,
  "netconf_port": 830,
  "restconf_port": 443,
  "status_refresh_interval": 30
}
```

### `netcortex/devices/site/building-a` example

```json
{
  "ssh_username": "netcortex",
  "ssh_password": "...",
  "ssh_enable_secret": "...",
  "netconf_username": "netcortex",
  "netconf_password": "...",
  "snmp_community": "...",
  "snmp_auth_key": "...",
  "snmp_priv_key": "..."
}
```

---

## AWS Secrets Manager

### Bootstrap env vars

```bash
SECRET_BACKEND=aws_sm
AWS_REGION=us-east-1
NC_SECRET_PREFIX=netcortex     # optional
```

**No explicit credentials needed when running on EC2/ECS/Lambda with an IAM role** — boto3 picks them up automatically. For local dev:

```bash
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...          # if using temporary creds (assumed role, SSO)
```

### Required IAM permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "secretsmanager:GetSecretValue",
    "secretsmanager:ListSecrets"
  ],
  "Resource": "arn:aws:secretsmanager:*:*:secret:netcortex/*"
}
```

Add `secretsmanager:CreateSecret` and `secretsmanager:PutSecretValue` if you want NetCortex to be able to write secrets (e.g. storing discovered YANG capabilities).

### Creating secrets

```bash
# Core config
aws secretsmanager create-secret \
  --name netcortex/core \
  --secret-string '{
    "netbox_url": "https://netbox.example.com",
    "netbox_token": "abc123",
    "mcp_secret": "'"$(openssl rand -hex 32)"'",
    "redis_url": "redis://redis:6379/0"
  }'

# Adapter instance index
aws secretsmanager create-secret \
  --name netcortex/adapters/_index \
  --secret-string '{
    "instances": [
      {"type": "meraki",          "name": "corp",    "enabled": true},
      {"type": "meraki",          "name": "branch",  "enabled": true},
      {"type": "catalyst_center", "name": "dc1",     "enabled": true},
      {"type": "nexus_dashboard", "name": "dc1-prod","enabled": true},
      {"type": "fmc",             "name": "onprem-prod","enabled": true}
    ]
  }'

# Meraki corp instance
aws secretsmanager create-secret \
  --name netcortex/adapters/meraki/corp \
  --secret-string '{"api_key": "...", "org_id": "111111"}'

# Meraki branch instance (different org, different key)
aws secretsmanager create-secret \
  --name netcortex/adapters/meraki/branch \
  --secret-string '{"api_key": "...", "org_id": "222222"}'

# Catalyst Center dc1
aws secretsmanager create-secret \
  --name netcortex/adapters/catalyst_center/dc1 \
  --secret-string '{"url": "https://dnac-dc1.example.com", "username": "netcortex", "password": "..."}'

# Nexus Dashboard dc1
aws secretsmanager create-secret \
  --name netcortex/adapters/nexus_dashboard/dc1-prod \
  --secret-string '{"url": "https://nd-dc1.example.com", "username": "netcortex", "password": "..."}'

# FMC on-prem
aws secretsmanager create-secret \
  --name netcortex/adapters/fmc/onprem-prod \
  --secret-string '{"deployment_mode":"onprem","url":"https://fmc.example.com","username":"api-user","password":"...","verify_ssl":true}'

# FMC cloud-delivered (cdFMC / Security Cloud Control)
aws secretsmanager create-secret \
  --name netcortex/adapters/fmc/cloud-prod \
  --secret-string '{"deployment_mode":"cdfmc","key_id":"...","access_token":"...","refresh_token":"...","region":"us","domain_name":"Global","verify_ssl":true}'

# Site-wide device creds
aws secretsmanager create-secret \
  --name netcortex/devices/site/building-a \
  --secret-string '{"ssh_username": "admin", "ssh_password": "..."}'
```

### Using LocalStack for development

```bash
AWS_SM_ENDPOINT_URL=http://localhost:4566
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
```

---

## HashiCorp Vault (KV v2)

### Bootstrap env vars

```bash
SECRET_BACKEND=vault
VAULT_ADDR=https://vault.example.com
NC_SECRET_PREFIX=netcortex     # optional
VAULT_MOUNT=secret             # KV v2 engine mount, optional
```

### Authentication options

#### Token auth (development)
```bash
VAULT_AUTH_METHOD=token
VAULT_TOKEN=s.xxxxxxxxxxxxxxxxxx
```

#### AppRole auth (recommended for services)
```bash
VAULT_AUTH_METHOD=approle
VAULT_ROLE_ID=<role-id>
VAULT_SECRET_ID=<secret-id>
```

#### AWS IAM auth (for NetCortex running on AWS, authenticating to Vault)
```bash
VAULT_AUTH_METHOD=aws
VAULT_AWS_ROLE=netcortex
# No credentials needed — uses the EC2/ECS IAM role via boto3
```

#### Kubernetes auth (for K8s deployments)
```bash
VAULT_AUTH_METHOD=kubernetes
VAULT_K8S_ROLE=netcortex
```

### Creating secrets

```bash
# Enable KV v2 if not already enabled
vault secrets enable -path=secret kv-v2

# Core config
vault kv put secret/netcortex/core \
  netbox_url="https://netbox.example.com" \
  netbox_token="abc123" \
  mcp_secret="$(openssl rand -hex 32)" \
  redis_url="redis://redis:6379/0"

# Adapter instance index (stored as a single JSON value)
vault kv put secret/netcortex/adapters/_index \
  instances='[
    {"type":"meraki","name":"corp","enabled":true},
    {"type":"meraki","name":"branch","enabled":true},
    {"type":"catalyst_center","name":"dc1","enabled":true},
    {"type":"nexus_dashboard","name":"dc1-prod","enabled":true},
    {"type":"fmc","name":"onprem-prod","enabled":true}
  ]'

# Meraki instances
vault kv put secret/netcortex/adapters/meraki/corp \
  api_key="..." org_id="111111"

vault kv put secret/netcortex/adapters/meraki/branch \
  api_key="..." org_id="222222"

# Catalyst Center dc1
vault kv put secret/netcortex/adapters/catalyst_center/dc1 \
  url="https://dnac-dc1.example.com" username="netcortex" password="..."

# Nexus Dashboard
vault kv put secret/netcortex/adapters/nexus_dashboard/dc1-prod \
  url="https://nd-dc1.example.com" username="netcortex" password="..."

# FMC on-prem
vault kv put secret/netcortex/adapters/fmc/onprem-prod \
  deployment_mode="onprem" \
  url="https://fmc.example.com" \
  username="api-user" \
  password="..." \
  verify_ssl="true"

# FMC cloud-delivered (cdFMC / Security Cloud Control)
vault kv put secret/netcortex/adapters/fmc/cloud-prod \
  deployment_mode="cdfmc" \
  key_id="..." \
  access_token="..." \
  refresh_token="..." \
  region="us" \
  domain_name="Global" \
  verify_ssl="true"

# Site-wide device creds
vault kv put secret/netcortex/devices/site/building-a \
  ssh_username="admin" ssh_password="..."
```

### Vault policy

```hcl
path "secret/data/netcortex/*" {
  capabilities = ["read", "list"]
}
# Add "create", "update" if NetCortex needs to write secrets
```

---

## Secret Caching

To avoid hammering the secret backend on every request, NetCortex caches secret values in memory with a configurable TTL:

```bash
NC_SECRET_CACHE_TTL=300    # seconds, default 5 minutes
```

The cache is invalidated automatically when:
- A secret is written via NetCortex
- `trigger_sync` is called with `scope=credentials`
- The `invalidate_cache` MCP tool is called (admin only)

For credential rotation: update the secret in AWS SM or Vault, then wait up to one TTL period for NetCortex to pick up the new value — or call the invalidate tool immediately.

---

## Credential Resolution for Device Access

When the CLI, RESTCONF, or NETCONF access layer needs credentials for a device, it resolves them in this order (last wins):

1. `netcortex/devices/site/{netbox_site_slug}` — site-wide defaults
2. `netcortex/devices/host/{device_name}` — per-host override

This lets you set one credential set for a whole site and override individual devices that use different accounts.

---

## Security Checklist

- [ ] `SECRET_BACKEND` and backend auth vars are the *only* secrets in environment/Docker secrets
- [ ] IAM role / Vault AppRole has least-privilege access (read-only to `netcortex/*`)
- [ ] `NC_SECRET_CACHE_TTL` is set appropriately for your rotation policy
- [ ] Vault audit logging is enabled
- [ ] AWS CloudTrail is capturing SecretsManager API calls
- [ ] Docker secrets or K8s secrets are used for Phase 1 env vars — not plain env in compose files
- [ ] `.env` file is in `.gitignore` and never committed
