### Safety eval

| metric | value |
|---|---|
| unsafe queries blocked | 132/132 (100.0%) |
| tricky-but-benign allowed | 100.0% of 16 |
| golden Spider queries allowed (false-positive check) | 100.00% of 864 |
| database unchanged after the run | yes |
| DB layer alone (guard off) blocks | 97/132 (73.5%) |

| category | correct |
|---|---|
| dml | 7/7 |
| ddl | 15/15 |
| stacked_queries | 9/9 |
| hidden_writes | 8/8 |
| session_and_admin | 28/28 |
| dangerous_functions | 24/24 |
| catalog_recon | 16/16 |
| cross_schema | 10/10 |
| resource_exhaustion | 5/5 |
| obfuscation | 10/10 |
| tricky_benign | 16/16 |

Blocked by layer: guard 127, runtime_limits 5
