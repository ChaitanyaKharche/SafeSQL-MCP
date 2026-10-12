### Accuracy eval (Spider dev, Postgres port)

| metric | value |
|---|---|
| model | `claude-haiku-5-5` (effort medium) |
| execution accuracy | **87.5%** (35/40) |
| answered | 100.0% |
| avg turns / tool calls | 3.15 / 2.4 |
| questions that hit a SQL error | 0 (recovered and correct: 0) |
| latency p50 / p95 | 4892.5 ms / 8009.7 ms |
| prompt tokens served from cache | 81.8% |
| cost per question | $0.00044 (total $0.0177) |
