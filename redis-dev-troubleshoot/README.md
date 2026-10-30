# Redis 問題排查（開發人員）Dashboard

給開發人員：應用程式遇到 Redis 相關問題（變慢、出錯、連不上、記憶體滿）或壓測時，依症狀找方向。每張圖的 ⓘ 都有「看什麼／正常／異常時」說明。

## 檔案

| 檔案 | 說明 |
|---|---|
| `redis-dev-troubleshoot-dashboard.json` | 匯入 Grafana 用的 Dashboard |
| `gen_dashboard.py` | 產生上面 JSON 的腳本。要改面板就改這裡，再執行 `python gen_dashboard.py redis-dev-troubleshoot-dashboard.json` 重新產生 |

第二個參數可以改「環境」資料來源的篩選 regex，預設 `Prometheus-(SIT|UAT|PROD-NH|PROD-BQ|PROD-BQ1)`：

```
python gen_dashboard.py redis-dev-troubleshoot-dashboard.json "Prometheus-.*"
```

## 匯入

1. Grafana → Dashboards → New → Import → 上傳 `redis-dev-troubleshoot-dashboard.json`。
2. 上方變數依序選：
   - **環境**：Prometheus 資料來源。
   - **Cluster**：redis_exporter 的 job。
   - **節點**：預設全選。
   - **主機 (node exporter)**：Redis 主機的 node exporter job（第 ⑦ 區用）。
3. 時間範圍要**包含出問題前**的一段時間，才看得出變化。

需求：redis_exporter（已用 v1.74.0 實測）、Grafana 12。

## 區塊

1. ① 健康總覽：有沒有東西壞掉
2. ② 流量與延遲：變慢先看這裡
3. ③ 錯誤與慢查詢：程式有沒有出錯
   - ③b Lua 腳本（EVAL / FCALL）：是不是腳本佔住 Redis、卡住其他指令
4. ④ 記憶體與 Key：會不會滿、資料會不會不見
5. ⑤ CPU、連線與帳號：瓶頸在哪、是哪個帳號
   - ⑤b 看起來沒滿、卻 command timeout：短暫爆滿與主機 / 網路瓶頸
6. ⑥ 複寫、Failover 與背景存檔（預設收合）
7. ⑦ 主機資源（node exporter，預設收合）

## 證明「Lua 腳本卡住 Redis」（③b）

Redis 一次只執行一個指令；一個 EVAL / EVALSHA / FCALL 從開始到結束，其他 client 的指令都只能排隊（不是被丟掉）。
腳本跑超過 `busy-reply-threshold`（舊名 `lua-time-limit`，預設 5 秒）時，其他指令會直接收到 `BUSY` 錯誤。

| 證據 | 面板 | 怎麼算 |
|---|---|---|
| **其他指令要排多久（最直接）** | 其他指令要排多久（exporter 實測） | `redis_exporter_last_scrape_duration_seconds`：exporter 每 15 秒下幾個簡單指令（INFO 等），它們跟其他 client 一樣要排隊。平常幾 ms，被佔住時變成秒級。Redis 沒有「佇列長度」指標，這就是排隊的實測值 |
| 主執行緒被腳本佔掉多少 | Lua 佔用主執行緒 | `rate(redis_commands_duration_seconds_total{cmd=~"eval.*\|fcall.*"})`：每秒有幾秒在跑腳本（含腳本裡 `redis.call` 的時間）。接近 100% = 其他指令都在排隊 |
| 每次腳本跑多久 | 單次腳本平均耗時、每次腳本耗時 vs 一般指令 | 腳本 ms～秒級、一般指令 µs 級；一般指令在 Redis 內的時間**不含排隊**，所以它正常但應用程式 timeout = 卡在腳本後面 |
| 其他指令被拒 | BUSY 錯誤 | `redis_errors_total{err="BUSY"}`，最直接的證據 |
| 其他指令在排隊 | 指令排隊的跡象 | client 輸入緩衝堆積、新建連線變多（等太久逾時重連） |

要知道是哪支腳本、誰呼叫的：`SLOWLOG GET`（含 client ip:port 與參數）、`CLIENT LIST`（`cmd=evalsha` 的連線）。Redis 沒有依帳號的指令統計。

## 看起來沒滿、卻 command timeout（⑤b）

圖表多半是 15 秒～1 分鐘的平均，Redis 是單執行緒，爆滿幾百毫秒就足以讓 timeout 設得短的 client 逾時；主機的瓶頸也常不在 Redis 指標裡：

- 先看 ③b「其他指令要排多久」：秒級 = Redis 在排隊；一直只有幾 ms = Redis 沒塞，問題在主機、網路或 client。
- **Redis 主執行緒 CPU 15 秒尖峰**：不被 1 分鐘平均蓋掉。
- **最忙的那一顆 CPU 核心**（含網路中斷 softirq）：主機 CPU 平均很低、但一顆核心滿了（網路中斷擠同一顆、docker-proxy 轉送）。
- **網卡使用率 ÷ 網卡速度**、**TCP 重傳率 / 連線佇列溢出 / conntrack**。需要上方選 Redis 主機的 node exporter；虛擬網卡常不回報速度。
- 都正常時，在 timeout 的那台跑 `redis-cli -h <redis> --latency-history -i 1`：PING 正常 = 問題在 client（連線池、執行緒、GC）。
- 要看每秒的主執行緒忙碌程度：`redis-cli info stats` 的 `instantaneous_eventloop_cycles_per_sec × instantaneous_eventloop_duration_usec ÷ 10⁶`（redis_exporter v1.74.0 沒輸出這組，只能手動看）。

## 容器記憶體上限 vs maxmemory（④）

maxmemory 只跟 Redis 自己算的資料用量（`used_memory`）比；Docker / K8s 的記憶體上限、OOM kill 看的是**實際佔用**（RSS），多了碎片和程式開銷：

- **實際佔用 ≈ 資料用量 × 碎片率**（常見 1.1～1.5）。④「用滿 maxmemory 時預估實際佔用」= maxmemory × 現在的碎片率，超過容器上限就代表資料還沒到 maxmemory，容器就先被 OOM kill。
- **背景存檔（RDB / AOF 重寫）**會 fork，存檔期間被改動的資料要多複製一份（copy-on-write），也算在容器上限裡；④ 表格「上次存檔多吃」就是這一塊。
- **單位**：Redis `30gb` = 30 GiB（`30g` 才是 30×10⁹）；Docker `32g` = 32 GiB。
- 一般建議：有開存檔、寫入多時，maxmemory 抓容器上限的 50～75%。
- **動態組 EVAL 腳本**（每次腳本內容都不一樣）會讓腳本快取一直長，④「Lua 腳本快取」看腳本數是否一直增加。

## 選用的 exporter / Redis 設定

- **`--export-client-list`**：⑤ 的「各帳號連線數」「帳號×來源主機」等面板需要；沒開會顯示提示。每條連線會產生多個 series，短連線多的環境 series 數會暴增，建議用 `metric_relabel_configs` 只保留 `redis_connected_client_info`。
- **`latency-monitor-threshold`**（例如 100 ms）：開了之後 ② 的「延遲尖峰（依類型）」「最近一次延遲尖峰」才有資料。
- **不要開 `--check-key-groups`**：會用 Lua 掃全部 key，key 多時會卡住主執行緒。

## 這裡看不到的

Redis 的指令統計、延遲、錯誤、命中率、慢查詢都是**整台全域**的，沒有依帳號的計數；能依帳號看的只有連線層級（數量、重連、大回應、來源主機）。以下要用 Redis 指令查：

- 慢查詢是哪個 key／來源：`SLOWLOG GET`
- 權限被拒是哪個帳號／key：`ACL LOG`
- 大 key、各前綴記憶體：`redis-cli --bigkeys` / `--memkeys`

真正依專案看流量與延遲，要靠應用程式端的 Redis client 埋點。
