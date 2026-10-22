# Spec: <feature title>

- Ticket: #<N>
- Author: <name or agent role>
- Status: draft | approved
- Size: S | M | L

## 1. Problem
What problem the feature solves, who has it, and what happens if we do nothing.

## 2. Users and context
- Primary users: <role>
- Secondary users: <role>
- Current workaround: <what they do today>

## 3. User stories
- As a <role>, I want <capability>, so that <outcome>.

## 4. Acceptance criteria (Gherkin)
```gherkin
Feature: <feature>

  Scenario: <happy path>
    Given <context>
    When <action>
    Then <observable outcome>

  Scenario: <edge or failure case>
    Given <context>
    When <action>
    Then <observable outcome>
```

## 5. Non-functional requirements

### Security
- OWASP ASVS target level, ticking one:
  - [ ] L1: low assurance; public or low-value data
  - [ ] L2: default for most apps handling personal or business data
  - [ ] L3: high value; payments, health, critical infrastructure
- Authentication / authorisation: <who can do what; deny by default>
- Input validation and output encoding: <boundaries>
- Secrets: <none in code or logs; where they live>
- Threat model required: yes / no (required when risk >= medium) → `docs/threats/<n>.md`

### Privacy
- Data classification: public | internal | confidential | personal | special-category
- Personal data collected, its purpose and its retention: <...>
- DPIA required: yes / no → `docs/dpia/<n>.md`

### Accessibility
- WCAG 2.2 AA: keyboard operable, visible focus, labels, contrast, no reliance on colour alone, works with screen readers.

### Performance
- Budgets: <e.g. p95 API latency < 300 ms; LCP < 2.5 s; bundle size +< 20 KB>

### Reliability
- SLO: <e.g. 99.9% monthly availability>; degradation behaviour when a dependency fails: <...>

### Observability
- Logs (no PII or secrets), metrics, traces, and alerts: <...>

### Internationalisation
- Locales, text externalised, date, number and currency formats: <...>

## 6. Dependencies
- <services, tickets (`After: #<n>`), external APIs, feature flags>

## 7. Out of scope
- <explicitly excluded>

## 8. Open questions
| # | Question | Proposed default | Answered by |
|---|---|---|---|
| 1 | | | |

## 9. Traceability
| Acceptance criterion | Test (file / name) | Level (unit / integration / e2e) |
|---|---|---|
| <scenario> | <path::test> | |
