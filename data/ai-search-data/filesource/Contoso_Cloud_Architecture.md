# Contoso Cloud Architecture Guide

*This fictional document is intended for demonstration purposes.*

## Architecture principles

Contoso uses a cloud-inclusive architecture that connects corporate locations, remote employees, partners, and cloud services. Engineering teams should apply these principles:

- **Zero Trust access:** Verify identity, device health, and authorization for every request. Use least-privilege access and managed identities where possible.
- **Resilience by design:** Deploy critical workloads across availability zones or regions, define recovery objectives, and test backups and failover procedures.
- **Secure connectivity:** Prefer private endpoints for sensitive services, encrypt traffic in transit, and separate production, test, and development networks.
- **Data protection:** Classify information before choosing storage, sharing, retention, encryption, and monitoring controls.
- **Operational visibility:** Centralize logs, metrics, traces, alerts, and security findings. Each service must have an owner and an incident response runbook.
- **Cost accountability:** Tag resources, set budgets, monitor utilization, and remove unused environments and data.
- **Automation:** Define infrastructure as code and use repeatable deployment pipelines with policy and security checks.

## Data sensitivity levels

Contoso assigns every data asset one of four sensitivity levels.

| Level | Classification | Examples | Required handling |
| --- | --- | --- | --- |
| 1 | Public | Published product information and approved press materials | May be shared externally after publication approval |
| 2 | Internal | Internal procedures, project plans, and non-sensitive operational data | Available only to authenticated employees and approved partners |
| 3 | Confidential | Customer records, employee data, contracts, and non-public financial information | Encrypt in transit and at rest; restrict access by role; audit access and sharing |
| 4 | Highly Confidential | Credentials, cryptographic keys, regulated data, acquisition plans, and critical security information | Use dedicated protected stores, strongest access controls, continuous monitoring, and explicit owner approval |

When multiple classifications apply, engineers must use the highest sensitivity level. Data owners review classifications at least annually and whenever a system's purpose or data changes.
