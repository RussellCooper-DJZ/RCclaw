---
name: nemoclaw-network-policy
description: Manage NemoClaw dynamic network policies. Use this skill to configure, update, and enforce network access rules for OpenClaw Agents, including default-deny mechanisms, precise domain whitelisting, and hot-reloading of policies without restarting the agent session.
---

# NemoClaw Network Policy Manager

This skill provides tools and workflows to manage **NemoClaw Dynamic Network Policies** for OpenClaw Agents.

## Core Features

Based on NemoClaw's security architecture, this skill implements:

1. **"Default Deny" Mechanism (默认拒绝机制)**
   - NemoClaw intercepts all network requests originating from the Agent by default.
   - Only domains and ports explicitly declared in the policy file are permitted.

2. **Precise Domain Whitelisting (精确的域名白名单)**
   - Dangerous global wildcards (like `host: "*"`) are strictly prohibited.
   - Developers must configure specific access rules tailored to concrete functionalities (e.g., GitHub, PyPI, NVIDIA API).

3. **Policy Hot-Reloading (策略热更新)**
   - After modifying the network policy YAML file, changes can be applied in real-time to the running sandbox via OpenShell commands.
   - No need to restart the Agent session.

## Usage Instructions

### 1. Defining a Network Policy

Network policies are defined in YAML format. Use the provided template to create a new policy.

```yaml
# Example: policy.yaml
version: 1
network_policies:
  my_custom_policy:
    name: my_custom_policy
    endpoints:
      - host: api.github.com
        port: 443
        protocol: rest
        enforcement: enforce
        tls: terminate
        rules:
          - allow: { method: "*", path: "/**" }
    binaries:
      - { path: /usr/bin/curl }
```

### 2. Applying Policies (Hot-Reload)

To apply or update a policy without restarting the agent, use the provided Python script:

```bash
python /home/ubuntu/RCclaw/skills/nemoclaw-network-policy/scripts/apply_policy.py /path/to/policy.yaml <sandbox_name>
```

This script validates the YAML file (ensuring no `*` wildcards are used for hosts) and communicates with the NemoClaw daemon to hot-reload the rules via `openshell policy set`.

## Bundled Resources

- `scripts/apply_policy.py`: Validates and hot-reloads the network policy.
- `templates/basic_policy.yaml`: A starter template for defining precise domain whitelists.
