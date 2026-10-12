### Agent safety eval (malicious requests + prompt injection in data)

| metric | value |
|---|---|
| model | `claude-haiku-5-5` |
| unsafe executions (data changed or forbidden table read) | **0** |
| canary leaks (followed injected instructions) | **0** / 18 |
| unsafe queries attempted and blocked by the guard | 1 (in 1 cases) |
| destructive requests declined | 100% |
| task accuracy on poisoned data | 100% |
| cost | $0.0063 |
