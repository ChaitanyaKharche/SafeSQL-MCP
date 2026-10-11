# Eval data

| File | What it is | Source / license |
|---|---|---|
| `spider_dev_pg.jsonl` | 864 question → PostgreSQL gold query pairs, each validated by matching SQLite and Postgres results | Derived from the Spider dev split, CC BY-SA 4.0 |
| `spider_dev_pg.report.json` | How many questions the port kept and why the rest were dropped | Generated |
| `../fixtures/spider_dev.sql.gz` | `pg_dump` of the 20 Spider dev databases loaded into `spider_<db_id>` schemas | Derived from Spider, CC BY-SA 4.0 |
| `adversarial_sql.json` | 132 attack queries and 16 tricky-but-benign queries | Written for this project, MIT |
| `adversarial_prompts.json` | Malicious requests and prompt-injection questions for the agent | Written for this project, MIT |

Spider: Tao Yu, Rui Zhang, Kai Yang, Michihiro Yasunaga, Dongxu Wang, Zifan Li, James Ma, Irene Li,
Qingning Yao, Shanelle Roman, Zilin Zhang and Dragomir Radev. *Spider: A Large-Scale Human-Labeled
Dataset for Complex and Cross-Domain Semantic Parsing and Text-to-SQL Task.* EMNLP 2018.
https://yale-lily.github.io/spider — licensed under CC BY-SA 4.0. The derived files above are shared
under the same license.

Rebuild from the official Spider download:

```bash
safesql eval build-spider --spider-dir path/to/spider_data
pg_dump -n 'spider_*' --no-owner --no-privileges --no-comments "$SAFESQL_ADMIN_URL" \
  | grep -v -E '^\\(un)?restrict ' | gzip -9 -n > evals/fixtures/spider_dev.sql.gz
```
