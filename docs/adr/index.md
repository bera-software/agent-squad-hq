# Architecture decision records

Each significant design decision is recorded as an ADR in the standard format: context, decision, consequences. New ADRs follow the process in [GOVERNANCE.md](https://github.com/bera-software/agent-squad-hq/blob/main/GOVERNANCE.md).

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-python-control-plane.md) | Python control plane | Accepted |
| [ADR-0002](0002-flows-yml-state-machine.md) | config/flows.yml is the state machine | Accepted |
| [ADR-0003](0003-single-config-file.md) | One user-facing configuration file | Accepted |
| [ADR-0004](0004-neutral-execution-result.md) | Provider and executor interfaces with a neutral execution result | Accepted |
| [ADR-0005](0005-agents-never-merge-or-deploy.md) | Agents never hold merge or deploy capability | Accepted |
| [ADR-0006](0006-deterministic-risk-floor.md) | Risk is the maximum of model risk and a path-based floor | Accepted |
| [ADR-0007](0007-untrusted-input.md) | All external text is untrusted, and trigger eligibility is decided by code | Accepted |
| [ADR-0008](0008-pluggable-state-store.md) | Pluggable state store | Accepted |
| [ADR-0009](0009-releases-and-pinning.md) | Releases, signing and pinning | Accepted |
| [ADR-0010](0010-open-core-boundary.md) | Open-core licensing boundary | Accepted |
| [ADR-0011](0011-token-broker-mode.md) | Token-broker mode: App tokens via GitHub OIDC | Accepted |
