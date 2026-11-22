---
type: EngineeringRule
title: "Core Engineering Invariants"
status: active
tags:
  - security
  - testing
  - platform
---

# Core Engineering Invariants

1. **TDD Red Compliance**: Always write tests that fail on baseline before writing implementation code.
2. **Sandbox Integrity**: Never modify configuration files or directories outside allowed target files.
3. **Cross-Platform Lifecycle**: Terminate process trees using process groups or Job Objects, and explicitly close database connections.
4. **AST Semantic Invariants**: Never introduce breaking changes to public signatures (removing parameters or adding required parameters without defaults) and avoid unsafe dynamic execution (`eval`, `exec`, reflection via `getattr` on sensitive modules).
