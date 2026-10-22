# Editions

Agent Squad HQ is open source under Apache-2.0. Two further editions are **planned and not built**; nothing on this page beyond the OSS column exists today.

**Open-core rule.** A capability belongs in the open-source edition if a solo developer or a small team needs it to be safe and productive. Security gates and the safety controls in the [security model](security/security-model.md) always stay open source. Paid editions add scale, governance and integrations, never basic safety ([ADR-0010](adr/0010-open-core-boundary.md)).

| Capability | OSS (available) | Cloud (planned) | Enterprise (planned) |
|---|---|---|---|
| Core pipeline: shape, design, implement, review, merge policy | Yes | Yes | Yes |
| Scheduled agents (Chief of Staff, briefs, debt scans, departments) | Yes | Yes | Yes |
| SSDLC gates (secrets, SAST, SCA, licences, IaC, SBOM) | Yes | Yes | Yes, plus policy as code |
| Stack packs | 17 in the repository | Yes | Yes, plus private packs |
| Skill catalogue | All 80 skills are in the repository; `pro` and `enterprise` tier skills are off by default, with no licence check | Yes | Yes |
| Runtime | GitHub Actions in your organisation; self-hosted server | Hosted GitHub App with hosted workers or dispatch into your Actions | Self-hosted or dedicated, your runners |
| Trackers | GitHub Issues (Actions mode); Jira Cloud, Linear, Trello, GitLab (server mode) | Yes | Plus enterprise trackers |
| Authentication for the API | Bearer tokens with viewer, approver and admin roles | Planned | SSO (SAML, OIDC), SCIM, fine-grained RBAC, approver groups |
| Audit log | Hash-chained log in server mode, with export | Yes | Plus SIEM export and evidence packs |
| Budgets | Monthly cap for hub loops | Per team and project | Budget hierarchy and chargeback |
| Dashboard and mobile app | Preview, self-hosted against your server | Hosted | Yes |
| Support | Community ([SUPPORT.md](https://github.com/bera-software/agent-squad-hq/blob/main/SUPPORT.md)) | Planned | Planned |

Plans, pricing and timing for the paid editions have not been announced. See the [roadmap](ROADMAP.md).
