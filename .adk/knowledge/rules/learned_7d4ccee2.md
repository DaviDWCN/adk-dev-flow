---
type: EngineeringRule
title: 'Learned Rule: fix(cpg): CodeGraph 默认走 sync_git 与 CAS 并加缓存版本'
status: active
source_run_id: 7d4ccee28a2c
paths:
- src/adk_dev_flow/ast_graph.py
- src/adk_dev_flow/cpg.py
- tests/test_sync_git_cas_cache.py
- README.md
- docs/cpg-cas-architecture.md
tags:
- learned
- auto-distilled
timestamp: '2026-11-26T12:11:26.698149+00:00'
---

# Learned Rule: fix(cpg): CodeGraph 默认走 sync_git 与 CAS 并加缓存版本

## Summary & Context
`CodeGraph` 默认不带 CAS 存储（`store=None`），`analyze_impact` 懒加载时走 `index_repo` 全量解析，CAS 与 `sync_git` 在生产路径上零调用方。`sync_git` 存在缓存投毒缺陷：以 HEAD 的 blob OID 作缓存键，未命中时优先读工作区文件，脏内容以干净 OID 入库。`CpgStore` 没有 schema 版本，缓存格式变更后旧库无法识别。

修复原则（用户已确认）：
- 缓存版本化选**整库重建**，不做行级迁移。
- 未命中一律 `git cat-file -p <oid>`，绝不读工作区。
- 非 git 仓库 / git 不可用时仍回退 `index_repo`（保持现有回退语义）。

## Resolution & Key Gotchas
- **Verdict Summary**: [AST 语义门禁一票否决] 发现危险函数调用 `subprocess.run` (line 635); 发现危险函数调用 `subprocess.run` (line 621); 发现危险函数调用 `subprocess.run` (line 697)
- **Resolved Failure Fingerprint**:
```
[Verification] ✅ 物理跑测全绿，正在执行 AST 静态门禁与交付入库
```
- **Resolved Blocking Issues**:
  - 发现危险函数调用 `subprocess.run` (line 635)
  - 发现危险函数调用 `subprocess.run` (line 621)
  - 发现危险函数调用 `subprocess.run` (line 697)
  - 发现敏感内建属性索引 `'__builtins__'` (line 1019)
  - 发现敏感内建属性索引 `'__builtins__'` (line 1021)
  - 发现敏感内建属性索引 `'__builtins__'` (line 1006)
  - 发现敏感内建属性索引 `'__builtins__'` (line 1011)
  - 发现危险函数调用 `subprocess.run` (line 26)
  - 检测到污点变量 `repo` 流入危险函数 `call:subprocess.run@2` (line 26)
