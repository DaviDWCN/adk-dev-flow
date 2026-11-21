---
type: DevSpec
title: "feat: example feature title"
status: active
issue_iid: 1
target_branch: main
require_tdd_red: true
extra_dependencies: []
allowed_paths:
  - "src/my_package"
acceptance_test_path: "tests/test_feature.py"
tasks:
  - id: "T1"
    title: "Implement core business logic"
    description: "Implement the required behavior in src/my_package/core.py"
    target_files:
      - "src/my_package/core.py"
---

# feat: example feature title

## Summary
Describe the feature requirements, behavior, and constraints here.

## Tasks Breakdown

### Task T1: Implement core business logic
Implement the required behavior in src/my_package/core.py
- **Target Files**: src/my_package/core.py

## Locked Acceptance Test (`tests/test_feature.py`)

```python
def test_feature_acceptance():
    # Pytest acceptance test verifying the requirement.
    # Must fail on baseline code when require_tdd_red is True.
    assert True
```
