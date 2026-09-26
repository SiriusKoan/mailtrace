# Docker Demo

## 啟動環境

```shell
uv sync --all-groups
docker compose -f demo/docker/docker-compose.yml up -d --build
```

主要服務與入口：

| 服務 | 主機名稱 | 主機連接埠 |
|---|---|---:|
| MX | `mx.example.com` | SMTP `10025` |
| Mailer 1 | `mailer1.example.com` | SMTP `20025` |
| Mailer 2 | `mailer2.example.com` | SMTP `20026` |
| Mailer 3 | `mailer3.example.com` | SMTP `20027` |
| Mailbox | `mailbox.example.com` | IMAP `10143` |
| OpenSearch | `opensearch.example.com` | HTTPS `9200` |
| Tempo | `tempo.example.com` | HTTP `3200`、OTLP gRPC `14317` |

## 郵件拓樸

從 MX 入口寄送：

```text
mx -> mailerN -> mailpolicyN -> mailbox
                             -> mx -> mailerM -> mailpolicyM -> mailbox  (alias 分支)
```

從 Mailer 入口寄送：

```text
mailerN -> mailpolicyN -> mailbox
                      -> mx -> mailerM -> mailpolicyM -> mailbox  (alias 分支)
```

每個 `mailpolicyN` 使用獨立的 Postfix `virtual_alias_maps`，不把公開測試
網域視為 local domain。一般 alias 目標屬於 `delivery.example.com`，由當前
`mailpolicyN` 直接送至 `mailbox`。`team` 的第二個 alias 目標屬於下一個
公開測試網域，因此會回到 `mx`，再經過下一組 `mailer` 與 `mailpolicy`。

| 收件者 | Alias 目標 |
|---|---|
| `single@N.example.com` | `user1@delivery.example.com` |
| `team@1.example.com` | `user1@delivery.example.com`、`alias-from-1@2.example.com` |
| `team@2.example.com` | `user1@delivery.example.com`、`alias-from-2@3.example.com` |
| `team@3.example.com` | `user1@delivery.example.com`、`alias-from-3@1.example.com` |

## Handoff 正確性驗證

`bench_handoff_correctness.py` 固定寄送六封郵件：

| 情境 | 收件者 | 預期路徑 | Mailbox |
|---|---|---|---|
| `mx-1-single` | `single@1.example.com` | `mx -> mailer1 -> mailpolicy1 -> mailbox` | `user1` |
| `mx-2-team` | `team@2.example.com` | `user1: mx -> mailer2 -> mailpolicy2 -> mailbox`<br>`user2: mx -> mailer2 -> mailpolicy2 -> mx -> mailer3 -> mailpolicy3 -> mailbox` | `user1`、`user2` |
| `mx-3-single` | `single@3.example.com` | `mx -> mailer3 -> mailpolicy3 -> mailbox` | `user1` |
| `mailer-1-team` | `team@1.example.com` | `user1: mailer1 -> mailpolicy1 -> mailbox`<br>`user2: mailer1 -> mailpolicy1 -> mx -> mailer2 -> mailpolicy2 -> mailbox` | `user1`、`user2` |
| `mailer-2-single` | `single@2.example.com` | `mailer2 -> mailpolicy2 -> mailbox` | `user1` |
| `mailer-3-team` | `team@3.example.com` | `user1: mailer3 -> mailpolicy3 -> mailbox`<br>`user2: mailer3 -> mailpolicy3 -> mx -> mailer1 -> mailpolicy1 -> mailbox` | `user1`、`user2` |

執行方式：

```shell
uv run python demo/docker/bench_handoff_correctness.py
```

腳本使用三份資料驗證 `mailtrace.tracing.builder.export_traces`：

- 從 mailbox 透過 IMAP 讀取原始郵件，解析 `Received` chain。
- 直接查詢 OpenSearch 原始文件，以獨立正規表示式解析 queue handoff。
- 使用正式的 query、group 與 `export_traces`，將輸出 span 投影成 handoff graph。

正確性不符時，腳本記錄失敗項目並回傳狀態碼 `0`；SMTP、IMAP、
OpenSearch 或設定錯誤等基礎設施問題回傳非零狀態碼。

## Resource benchmark

Resource benchmark Compose 是主 Compose 的覆寫檔：

```shell
docker compose \
  -f demo/docker/docker-compose.yml \
  -f demo/docker/docker-compose.resource-benchmark.yml \
  up -d --build
```

覆寫檔只調整隔離網段、主機連接埠、healthcheck、Vector 設定與
mailtrace daemon。`bench_resource_rates.py` 會自動使用這兩個 Compose 檔案。

## 其他流量產生器

```shell
demo/docker/send_bulk_emails.sh 100
uv run python demo/docker/send_bulk_emails.py 20 60
```

兩個工具都會在上述六種路徑之間分配郵件。
