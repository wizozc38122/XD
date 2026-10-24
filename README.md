# OCP 壓測影響評估 Dashboard

在 OpenShift 上壓測某個 Deployment 時，用來判斷它會不會拖慢**同一台 Node** 上的其他服務。

Pod 已經有 CPU / 記憶體 limit，但下面這些是**整台 Node 共用、limit 管不到**的資源，也是這個 Dashboard 的重點：

- 本機磁碟 IO（寫 log、暫存檔、容器可寫層）
- 本機磁碟空間
- 網卡頻寬與封包處理
- conntrack 連線追蹤表

## 檔案

| 檔案 | 說明 |
|---|---|
| `ocp-loadtest-impact-dashboard.json` | 匯入 Grafana 用的 Dashboard |
| `gen_dashboard.py` | 產生上面 JSON 的腳本。要改面板就改這裡，再執行 `python gen_dashboard.py` 重新產生 |

## 匯入

1. Grafana → Dashboards → New → Import → 上傳 `ocp-loadtest-impact-dashboard.json`。
2. 上方變數依序選：
   - **資料來源**：OCP 的 Prometheus 或 Thanos Querier。
   - **Namespace**、**Deployment**：要壓測的服務。
   - **Node**：會自動帶出該 Deployment 的 Pod 所在的 Node，預設全選。
3. 時間範圍要**包含壓測前**的一段時間，才看得出壓測時有沒有往上飆。

## 先搞清楚「在看誰」

每個面板標題都有前綴，代表這個面板看的對象。**這是看懂這個 Dashboard 最重要的一點。**

| 前綴 | 看的是 | 用來回答 |
|---|---|---|
| 【他】 | 他自己的 Pod 合計 | 他用了多少、有沒有被自己的 limit 卡住 |
| 【占比】 | 他 ÷ 所在 Node 總量 | 他在這台 Node 上算不算大戶 |
| 【整台 Node】 | 整台 Node 所有 Pod 加總，**不是他的用量** | 這台機器有沒有變吃緊 |
| 【鄰居】 | 同 Node 上所有 Pod 的排名（含他自己） | 誰是大戶、鄰居有沒有跟著變化 |
| 【共用】 | 全叢集共用元件（Router、DNS） | 入口和 DNS 有沒有被打高 |

【整台 Node】的指標**沒辦法告訴你是誰造成的**，它只回答「這台機器有沒有變吃緊」。要判斷是不是他造成的，要把三個面向的時間對起來：

1. 【他】的用量在壓測時上升；
2. 【整台 Node】的壓力指標在**同一時間**往上飆；
3. 【鄰居】排名裡他是前幾名，或其他 Pod 的狀況跟著變差。

三者都成立，才算他影響了整體。只有占比高，不能下結論。

## 為什麼看的是「他所在的 Node」

磁碟、網卡、conntrack 都是**每台 Node 各自一份**，所以他只會直接影響和他跑在同一台 Node 上的 Pod。這也是 `Node` 變數會自動帶出他所在 Node 的原因。

有幾個例外，影響範圍會超出他所在的 Node：

- **Pod 會移動**：HPA 擴容或 Pod 重建時，可能被排到新的 Node。第 1 區「各 Node 上的 Pod 數」可以看到部署位置隨時間的變化，新出現的 Node 也要一起看。
- **PVC 用網路儲存**（Ceph、NFS、SAN）：IO 會打到共用的儲存後端，影響所有用同一個儲存的服務，不限同一台 Node。這部分要看儲存系統自己的監控。
- **入口與 DNS**：Router 與 CoreDNS 是全叢集共用，所以放在第 6 區。
- **外部服務**（DB、Kafka、Redis）：連線數與負載會隨他的副本數放大，要在各系統自己的監控對照同一個時間窗。

## 各區塊說明

### 1. 總覽：他自己有沒有被限制卡住、部署在哪

快速看他的副本數、CPU 用量 ÷ request、記憶體 ÷ limit、CPU throttling、重啟次數，以及他占所在 Node 的 CPU 比例。下方有 Pod 分布在哪些 Node 的圖和對應表。

### 2. 【他】自己的用量

他的 CPU、記憶體、網路流量、網路丟包、磁碟讀寫、可寫層用量、重啟與 OOMKilled。CPU 面板會疊上「昨天同時段」，用來確認這次壓測多出多少負載。

### 3. 【占比】他 ÷ 所在 Node 總量

CPU、網路、磁碟寫入、記憶體各占所在 Node 的比例。只計算跑在所選 Node 上的 Pod。

### 4. 【整台 Node】共用資源壓力

這一區是**整台 Node 的狀態**，不是他的用量。看壓測前和壓測中的曲線有沒有明顯往上飆。

### 5. 【鄰居】所選 Node 上用量前 10 名

第 4 區往上飆的時候，看是誰同時上升。

### 6. 【共用】入口與 DNS

Router 的回應狀態碼、後端排隊，以及 CoreDNS 的查詢量與延遲。

## 指標說明

### PSI（Pressure Stall Information）

Linux 核心記錄的「有工作因為搶不到資源而在等」的時間比例，分成 CPU、IO、記憶體三種。它比 CPU 使用率更直接回答「大家有沒有變慢」：CPU 使用率 90% 但沒人在等，其實沒問題；PSI 上升才代表有工作真的被卡住。

- **怎麼看**：壓測前後比較，往上飆就是 Node 開始吃緊。粗略參考：CPU 或 IO 持續超過 10% 值得注意。
- **沒有資料**：Node 核心沒開 PSI。RHCOS 預設可能關閉，需要加 `psi=1` 核心參數才會有。這時改看「PSI 替代：iowait 與每核負載」面板。

### iowait 與每核負載（PSI 的替代）

- **iowait**：CPU 閒著在等磁碟的時間比例。往上飆代表磁碟變慢。
- **每核負載**：`load1 ÷ CPU 核數`，代表平均每顆核有幾個工作在排隊。長時間超過 100% 代表工作在排隊。

### 磁碟延遲（await）

每次讀寫平均要等多久。**判斷磁碟有沒有拖慢大家，這個最準。** 粗略參考：SSD 通常數 ms 以內，超過 10–20 ms 就很慢。

### 磁碟忙碌程度（io time / %util）

磁碟有在處理 IO 的時間比例。接近 100% 可能飽和，但 SSD 和雲端磁碟可以同時處理多筆 IO，100% 不一定代表慢，要搭配磁碟延遲一起看。

### IOPS

每秒完成的讀寫次數。IOPS 上升而延遲沒變，代表磁碟還撐得住；IOPS 上不去但延遲飆升，代表已經到磁碟上限。

### 檔案系統使用率與 DiskPressure

Node 本機磁碟空間用了幾成，容器映像、容器可寫層、log 都放在這裡。剩餘空間太少時，kubelet 會把 Node 標成 DiskPressure 並開始驅逐 Pod，同 Node 的其他服務也會被趕走。超過 85% 就要注意。

### 網路丟包 / 錯誤

- **丟包（drop）**：網卡或核心的緩衝區塞滿、處理不及，封包被丟掉。
- **錯誤（error）**：封包損壞或硬體問題。

兩者正常都應接近 0。壓測期間開始出現並往上升，代表網卡塞住，會造成重傳與延遲變高，同 Node 所有 Pod 都受影響。

### conntrack 連線追蹤表

Node 核心記錄每條網路連線的表格，同 Node 所有 Pod 共用一張，有容量上限。大量短連線（例如每次都重新連 DB、Redis、Kafka）最容易吃滿。超過 70% 要注意；滿了之後新連線會被直接丟掉，同 Node 所有 Pod 都會連不出去。

### CPU throttling

Pod 想用 CPU 但被 limit 擋下的時間比例。超過 25% 通常已經讓他自己變慢。**被節流只影響他自己，不會拖累鄰居**，但可能引發 timeout 和重試，間接放大對外部服務的流量。

### CPU 用量 ÷ Request

超過 100% 代表他正在借用 Node 的閒置 CPU。Node 吃緊時，超用 request 的 Pod 最容易和鄰居互相搶。

### 記憶體 working set 與 OOMKilled

working set 是 Linux 判斷要不要 OOM 的記憶體用量。碰到 memory limit 會被 OOMKilled，只影響他自己的 Pod。

## 限制與沒資料時的處理

| 狀況 | 原因與處理 |
|---|---|
| PSI 面板沒資料 | Node 核心沒開 PSI，改看替代面板 |
| 所有【整台 Node】面板都沒資料 | 這些查詢假設 node-exporter 的 `instance` 標籤是 Node 名稱（OCP 預設如此）。如果你的環境是 `IP:9100`，要修改查詢 |
| 網路相關面板沒資料 | 網卡名稱不是 `ens / eth / eno / enp / bond` 開頭，修改 `gen_dashboard.py` 的 `NIC` |
| 磁碟相關面板沒資料 | 磁碟名稱不是 `sd* / nvme* / vd* / xvd*`，修改 `gen_dashboard.py` 的 `DEV` |
| 找不到 Deployment 的 Pod | 用 Pod 名稱規則 `<deployment>-<hash>-<id>` 對應，StatefulSet、DaemonSet 不適用 |
| 網路占比超過 100% | 同一台 Node 內 Pod 互傳不經過實體網卡，占比只當量級參考 |
| 磁碟數字對不太上 | 容器層（cAdvisor）與 Node 層的統計方式不同，寫到 PVC 的量也不算在 Node 本機磁碟，只當量級參考 |
| Router 面板沒資料 | 服務不經過 Router，或沒有權限查詢 Router 指標 |
