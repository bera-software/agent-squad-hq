# Threat model: <feature or component>

- Ticket / design PR: #<N>
- ADR: <docs/adr/NNNN-...>
- Author: <name or agent role>
- Date: <YYYY-MM-DD>
- Status: draft | reviewed | accepted

## 1. Scope
What is being modelled, what changed, and why a threat model is needed (risk >= medium, a new trust boundary, auth, PII, payments, LLM features).

## 2. Data flow
Describe or diagram the components, the data stores, the external actors, and every trust boundary that data crosses.

| # | Flow | Source → destination | Data (classification) | Crosses trust boundary? | Auth / transport |
|---|---|---|---|---|---|
| F1 | <e.g. user submits form> | <browser → API> | <PII / internal / public> | yes / no | <session cookie, TLS> |

## 3. STRIDE per component or boundary

| Component / boundary | Threat category | Threat | Likelihood (L/M/H) | Impact (L/M/H) | Mitigation | Status | Ticket |
|---|---|---|---|---|---|---|---|
| <component> | Spoofing | <who could impersonate whom> | | | | open / mitigated / accepted | #<n> |
| <component> | Tampering | <data or code modified in transit or at rest> | | | | | |
| <component> | Repudiation | <action without an audit trail> | | | | | |
| <component> | Information disclosure | <data leak, verbose errors, logs> | | | | | |
| <component> | Denial of service | <resource exhaustion, unbounded input> | | | | | |
| <component> | Elevation of privilege | <authz bypass, IDOR, injection> | | | | | |

## 4. OWASP Top 10 for LLM Applications (2025)
Fill this in if the feature uses an LLM, an agent, or tool calling. Otherwise write "not applicable".

| ID | Risk | Applies? | Threat in this feature | Mitigation | Ticket |
|---|---|---|---|---|---|
| LLM01 | Prompt Injection | yes / no | <untrusted text reaching the model> | <fencing, no instruction-following from data, deterministic gates> | |
| LLM02 | Sensitive Information Disclosure | | | | |
| LLM03 | Supply Chain | | <models, plugins, packages, pinned versions> | | |
| LLM04 | Data and Model Poisoning | | | | |
| LLM05 | Improper Output Handling | | <model output used in SQL, shell, HTML, URLs> | | |
| LLM06 | Excessive Agency | | <tools, permissions, autonomy beyond need> | | |
| LLM07 | System Prompt Leakage | | <secrets or policy kept in prompts> | | |
| LLM08 | Vector and Embedding Weaknesses | | | | |
| LLM09 | Misinformation | | <users relying on unverified output> | | |
| LLM10 | Unbounded Consumption | | <cost, token or rate abuse> | | |

## 5. Assumptions
- <e.g. TLS terminates at the load balancer>

## 6. Out of scope
- <e.g. physical security, the hosting provider's internals>

## 7. Residual risk
What is still open after the mitigations, and why it is acceptable. Name who accepts it.

## 8. Sign-off
| Role | Name | Decision | Date |
|---|---|---|---|
| Security reviewer | | approve / changes needed | |
| Owner / approver | | accept residual risk | |
