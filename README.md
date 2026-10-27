# OCP 壓測影響評估 Dashboard

在 OpenShift 上壓測某個 Deployment 時，用來判斷它會不會拖慢**同一台 Node** 上的其他服務。

Pod 已經有 CPU / 記憶體 limit，但下面這些是**整台 Node 共用、limit 管不到**的資源，也是這個 Dashboard 的重點：

- 本機磁碟 IO（寫 log、暫存檔、容器可寫層）
- 本機磁碟空間
- 網卡頻寬與封包處理（核心處理封包的 CPU 也不算在 Pod 的 CPU limit 裡）
- conntrack 連線追蹤表

OCP 沒有特別設定時，不會限制 Pod 的網路頻寬與磁碟讀寫速度，一個 Pod 可以用到整台 Node 的上限。

## 兩個 Dashboard 的分工

| Dashboard | 用途 |
|---|---|
| **OCP 叢集風險總覽** | 全叢集每台 Node、每個 Workload 的排行，先找出嫌疑對象 |
| **OCP 壓測影響評估** | 針對單一 Deployment，查證它有沒有拖慢所在 Node 上的其他服務 |

使用流程：在總覽看哪台 Node 變吃緊、哪個 Workload 排名前面 → 點表格中的 `workload` 名稱 → 自動帶著 namespace、workload 與時間範圍開啟細節 Dashboard。細節 Dashboard 右上角也有連結可以回到總覽。

## 檔案

| 檔案 | 說明 |
|---|---|
| `ocp-cluster-risk-overview-dashboard.json` | 叢集風險總覽 |
| `ocp-loadtest-impact-dashboard.json` | 單一 Workload 細節 |
| `gen_overview_dashboard.py`、`gen_dashboard.py` | 產生上面兩個 JSON 的腳本。要改面板就改腳本，再執行 `python <腳本>` 重新產生 |

## 百分比一定要看分母

同樣是「80%」，分母不同意思就完全不同。兩個 Dashboard 的每個百分比，標題或欄位名稱都會寫明「÷ …」：

| 分母 | 例子 | 回答的問題 |
|---|---|---|
| **÷ 自己的 Request / Limit** | 【他】CPU 用量 ÷ 自己的 Request | 他有沒有超出自己的設定 |
| **÷ Node 目前用量** | 【他】CPU ÷ Node 目前 CPU 用量 | 他是不是這台 Node 的大戶 |
| **÷ Node 容量** | 【他】CPU ÷ Node 總核數、【整台 Node】CPU ÷ 總核數 | 資源離滿還有多遠 |

這裡的用量都是**實際使用量**，不是 request 預留量；容量是 Node 的硬體總量（總核數、總記憶體），不是扣掉系統保留後的可分配量。

**÷ Node 目前用量很容易誤判**：壓測時如果其他服務很閒或停掉，只剩他在跑，這個比例一定接近 100%，但資源可能其實很空。**要判斷資源快不快滿，看 ÷ Node 容量。**

## 叢集風險總覽

一個資料來源對應一個 OCP 叢集，切換「資料來源（叢集）」就是切換叢集。

| 區塊 | 內容 |
|---|---|
| 1.【Node】各 Node 狀態 | 每台 Node 的 CPU 使用率（÷ 總核數）、記憶體使用率（÷ 總記憶體）、conntrack（÷ 表格上限）、磁碟延遲、磁碟空間（÷ 容量）、網路丟包、iowait、網路封包處理 CPU、壓力狀態，加上各項 Top 10 趨勢 |
| 2.【Workload】CPU、記憶體 | 各一張表，依序 **用量 → 佔 Node → Request → 用量 ÷ Request → Limit → Limit 設定 → 用量 ÷ Limit**（CPU 表多一欄 throttling） |
| 2.【Workload】沒設 Limit 的容器 | 每列一個容器：沒設哪個 Limit（CPU / 記憶體）、用量、Request、用量 ÷ Request |
| 2.【Workload】Pod 之間分配不均 | 2 個 Pod 以上的 Workload：CPU、網路最忙 Pod 佔全部幾 %、是平均的幾倍；記憶體最多的 Pod 是平均的幾倍 |
| 2.【Workload】共用資源與重啟 | 重啟、網路流量與丟包、磁碟寫入、可寫層用量 |
| 3.【Workload】Limit 相關 Top 10 | CPU / 記憶體用量 ÷ Limit、throttling、重啟隨時間的變化 |
| 4.【Workload】共用資源 Top 10 | 沒設 Limit 的容器 CPU、記憶體用量，網路、磁碟這些 limit 管不到、會影響同 Node 鄰居的指標隨時間的變化 |

注意事項：

- **表格是快照**：表格取的是時間範圍**結束前 5 分鐘**。要看壓測當下的排名，把時間範圍的結束點拉到壓測期間；Top 10 趨勢圖則會顯示整段時間。
- **風險排行的角度是整個 Workload**：用量、Request、Limit 是所有 Pod 加總。例外是會讓單一容器出事的比例取最糟的那一個，因為加總會把快爆的平均掉：
  - **用量 ÷ Limit**：最接近 Limit 的那個容器。CPU 接近 100% 會被 throttling，記憶體到 100% 會被 OOMKilled。
  - **throttling**：被節流最嚴重的 Pod。
  - **用量 ÷ Request**：只算有設 Request 的容器（沒設 Request 的容器用量不會灌進別人的比例裡）。超過 100% 代表正在用 Node 上別人沒用到的資源，Node 一忙就會互搶。
- **佔 Node**：他在某台 Node 上的用量（同一台的 Pod 加總）÷ 那台 Node 的總量（總核數、總記憶體），取佔最多的那台。用來判斷他是不是那台 Node 的大戶；那台 Node 整體還剩多少，看第 1 區 Node 表。用量很高但佔 Node 很小 → 對 Node 影響不大。
- **設成 0 = 沒設**：Request / Limit 設 `0` 在 Kubernetes 等於沒設，dashboard 也當成沒設（以前會除以 0 變成無限大 %）。
- **Limit 設定**：都有設 / 部分容器沒設 / 全部沒設。部分容器沒設時，Limit 欄只加總有設的容器；是哪個容器沒設，看「沒設 Limit 的容器」表，每列一個容器。
- **Pod 之間分配不均**：同一個 Workload 的 Pod 應該分到差不多的量。最忙 Pod ÷ 平均 > 2.5（紅）代表流量集中在少數 Pod，檢查 Service / LB、長連線（gRPC、HTTP keep-alive）是否都黏在同一個 Pod、message key 或分片是否集中。用量太少（CPU < 0.05 核、網路 < 10 KB/s）不算。
- **表格格子是灰字**（—、沒設 Limit、沒設 Request、沒收集）：那一格算不出來，文字就是原因；欄位不會因為沒資料而消失。
- **排名高不等於造成影響**：只代表是大戶，要進細節 Dashboard 對照 Node 壓力的時間點。
- **預設排除系統 namespace**：`openshift.*|kube-.*`，可在上方「排除的 Namespace」修改或清空。
- **點擊跳轉只支援 Deployment**：細節 Dashboard 以 Pod 名稱規則對應 Deployment，StatefulSet、DaemonSet 點進去會沒有資料。
- **使用 hostNetwork 的 Pod**：網路指標會是整台 Node 的流量，排名可能偏高。

## 匯入

1. Grafana → Dashboards → New → Import，分別上傳兩個 JSON。兩個都要匯入，互相跳轉的連結才會有效。
2. 上方變數依序選，**不需要填任何數值**：
   - **資料來源**：只會列出名稱符合 `Prometheus.*OCP`（開頭 `Prometheus`、結尾 `OCP`）的 Prometheus 資料來源。要改規則，修改腳本裡 datasource 變數的 `regex`。
   - **Namespace**、**Deployment**：要壓測的服務。
   - **Node**：會自動帶出該 Deployment 的 Pod 所在的 Node，預設全選。
3. 時間範圍的**起點要設在壓測開始前**：才看得出壓測時有沒有往上飆，3C、3E 的「壓測前 7 天峰值」也才不會被壓測本身墊高。

## 先搞清楚「在看誰」

每個面板標題都有前綴，代表這個面板看的對象，**一張圖只看一個對象**，他和整台 Node 不會畫在同一張圖。這是看懂這個 Dashboard 最重要的一點。

| 前綴 | 看的是 | 用來回答 |
|---|---|---|
| 【他】 | 他自己的 Pod（**主要看這個**） | 他用了多少、有沒有被自己的 limit 卡住、占 Node 多少 |
| 【整台 Node】 | 他所在 Node 的所有 Pod 加總，**不是他的用量** | 這台機器有沒有變吃緊 |
| 【鄰居】 | 同 Node 上所有 Pod 的排名（含他自己） | 誰是大戶、鄰居有沒有跟著變化 |
| 【共用】 | 全叢集共用元件（Router、DNS） | 入口和 DNS 有沒有被打高 |

【整台 Node】的指標**沒辦法告訴你是誰造成的**，它只回答「這台機器有沒有變吃緊」。要判斷是不是他造成的，要把三個面向的時間對起來：

1. 【他】的用量在壓測時上升；
2. 【整台 Node】的壓力指標在**同一時間**往上飆；
3. 【鄰居】排名裡他是前幾名，或其他 Pod 的狀況跟著變差。

三者都成立，才算他影響了整體。只有「÷ Node 目前用量」高，不能下結論。

## 為什麼看的是「他所在的 Node」

磁碟、網卡、conntrack 都是**每台 Node 各自一份**，所以他只會直接影響和他跑在同一台 Node 上的 Pod。這也是 `Node` 變數會自動帶出他所在 Node 的原因。

有幾個例外，影響範圍會超出他所在的 Node：

- **Pod 會移動**：HPA 擴容或 Pod 重建時，可能被排到新的 Node。第 1 區「各 Node 上的 Pod 數」可以看到部署位置隨時間的變化，新出現的 Node 也要一起看。
- **PVC 用網路儲存**（Ceph、NFS、SAN）：IO 會打到共用的儲存後端，影響所有用同一個儲存的服務，不限同一台 Node。這部分要看儲存系統自己的監控。
- **入口與 DNS**：Router 與 CoreDNS 是全叢集共用，所以放在第 6 區。
- **外部服務**（DB、Kafka、Redis）：連線數與負載會隨他的副本數放大，要在各系統自己的監控對照同一個時間窗。

## 各區塊說明

### 1. 總覽：他自己有沒有被限制卡住、部署在哪

快速看他的副本數、CPU 用量 ÷ 自己的 Request、記憶體 ÷ 自己的 Limit、CPU throttling、重啟次數，以及他的 CPU ÷ 所選 Node 總核數。下方有 Pod 分布在哪些 Node 的圖和對應表。

### 2. 【他】自己的用量

他的 CPU、記憶體、網路流量、網路丟包、磁碟讀寫、可寫層用量、重啟與 OOMKilled。CPU 面板會疊上「昨天同時段」，用來確認這次壓測多出多少負載。

### 3A. 【他】÷ Node 目前用量

CPU、記憶體、網路、磁碟寫入，每台 Node 一條線：這台 Node 現在用掉的資源，有幾成是他用的。**只說明他是不是大戶，不代表資源快滿。** 只剩他在跑時一定接近 100%；Node 總共只跑 10 MB/s 時，他占 80% 也只有 8 MB/s，網卡其實很閒。

### 3B. 【他】÷ Node 容量（主要看這區）

他自己用掉所在 Node 容量的幾成，每台 Node 一條線。其他服務停掉也不會被灌高，是「他用掉多少資源」的真實比例。所有容量都自動取得，不需要輸入：

| 面板 | 容量從哪裡來 |
|---|---|
| CPU | Node 的 CPU 總核數 |
| 記憶體 | Node 的總記憶體 |
| 網路（送出 TX、接收 RX 各一條線） | Node 回報的網卡速度（`node_network_speed_bytes`） |
| 磁碟忙碌程度 | 總時間。這是**估算值** = 整台磁碟忙碌率 × 他占目前磁碟讀寫量的比例 |
| 磁碟空間 | 檔案系統容量 |

磁碟的讀寫速度與 IOPS 上限沒有任何指標會回報，所以改用「忙碌時間 ÷ 總時間」代表磁碟用掉多少。SSD 和雲端磁碟可以同時處理多筆 IO，到 100% 不一定真的慢，要搭配第 4 區「磁碟延遲」一起看。

**虛擬機的網卡速度不一定準**：VMware 的 vmxnet3 通常固定回報 10 Gbps，實際可用頻寬可能更低，所以網卡的線不高時，也請搭配第 4 區「網路丟包」一起看。知道實際頻寬時，維護者可以在網址加上 `&var-nic_mbps=<Mbps>` 覆蓋（這是隱藏變數，畫面上不會出現）。網卡用 bond 時，bond 和底下的實體網卡都會被算進流量，使用率可能偏高。

### 3C. 【他】目前 vs 他自己壓測前 7 天峰值

解決「看到他很高，但不知道算不算高」的問題。CPU、記憶體、網路送出、網路接收、磁碟讀寫、可寫層用量，每張圖兩條線，都是他所有 Pod 合計：

- **他 目前**（實線）：實際用量，單位是核數、bytes、bits/s。
- **他 壓測前 7 天峰值**（虛線）：時間範圍**開始前** 7 天內他自己的最高值（每 5 分鐘平均）。

實線遠高於虛線，代表這次壓測讓他用到平常沒到過的量。

### 3D. 【整台 Node】÷ 容量

他所在的 Node 快不快滿，每台 Node 一條線：CPU ÷ 總核數、記憶體 ÷ 總記憶體、網路 ÷ 網卡上限、磁碟忙碌程度 ÷ 總時間、磁碟空間 ÷ 檔案系統容量。接近紅色虛線代表這項資源快滿，同 Node 所有 Pod 都會受影響。和 3B 對照：3B 他的比例越接近這裡，代表用量主要來自他。

### 3E. 【整台 Node】目前 vs 整台壓測前 7 天峰值

CPU、記憶體、網路送出、網路接收、磁碟吞吐、磁碟 IOPS，每台 Node 一組實線（目前）和虛線（這台 Node 壓測前 7 天的最高值）。實線超過同一台 Node 的虛線，代表比過去一週最忙的時候還忙，這台 Node 正在承受平常沒遇過的壓力；在虛線以下，代表還在平常會遇到的範圍內，通常還好。

下方的參考表列出每台 Node 回報的網卡速度，以及到時間範圍結束為止、最近 7 天的峰值。網卡速度遠高於峰值，代表網卡還有很多餘裕。峰值只代表「至少撐得住這麼多」，不是上限：峰值那段時間的磁碟延遲與網路丟包都正常，就代表上限更高。

要確認硬體規格，也可以直接在 Node 上查：

```bash
oc debug node/<node名稱> -- chroot /host cat /sys/class/net/<網卡名稱>/speed
```

```bash
oc debug node/<node名稱> -- chroot /host lsblk -d -o NAME,ROTA,SIZE,MODEL
```

第一個回傳網卡速度（Mbps）；第二個列出本機磁碟，`ROTA=0` 是 SSD。

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

磁碟有在處理 IO 的時間 ÷ 總時間。接近 100% 可能飽和，但 SSD 和雲端磁碟可以同時處理多筆 IO，100% 不一定代表慢，要搭配磁碟延遲一起看。

### IOPS

每秒完成的讀寫次數。IOPS 上升而延遲沒變，代表磁碟還撐得住；IOPS 上不去但延遲飆升，代表已經到磁碟上限。

### 檔案系統使用率與 DiskPressure

Node 本機磁碟空間用了幾成（÷ 容量），容器映像、容器可寫層、log 都放在這裡。剩餘空間太少時，kubelet 會把 Node 標成 DiskPressure 並開始驅逐 Pod，同 Node 的其他服務也會被趕走。超過 85% 就要注意。

### 網路封包處理 CPU（softirq）

Node 核心處理網路封包（以及部分磁碟完成通知）所花的 CPU 時間比例。這部分 CPU **不算在 Pod 的 CPU limit 裡**，所以就算他的 CPU 被限制住，大流量還是會吃掉整台 Node 的 CPU。壓測時跟著網路流量一起往上飆，就是大流量在消耗 Node 的 CPU。持續超過 10% 值得注意。

### 網路丟包 / 錯誤

- **丟包（drop）**：網卡或核心的緩衝區塞滿、處理不及，封包被丟掉。
- **錯誤（error）**：封包損壞或硬體問題。

兩者正常都應接近 0。壓測期間開始出現並往上升，代表網卡塞住，會造成重傳與延遲變高，同 Node 所有 Pod 都受影響。

### conntrack 連線追蹤表

Node 核心記錄每條網路連線的表格，同 Node 所有 Pod 共用一張，有容量上限。大量短連線（例如每次都重新連 DB、Redis、Kafka）最容易吃滿。超過表格上限的 70% 要注意；滿了之後新連線會被直接丟掉，同 Node 所有 Pod 都會連不出去。

### CPU throttling

Pod 想用 CPU 但被 limit 擋下的時間比例。超過 25% 通常已經讓他自己變慢。**被節流只影響他自己，不會拖累鄰居**，但可能引發 timeout 和重試，間接放大對外部服務的流量。

### CPU 用量 ÷ 自己的 Request

超過 100% 代表他正在借用 Node 的閒置 CPU。Node 吃緊時，超用 request 的 Pod 最容易和鄰居互相搶。

### 記憶體 working set 與 OOMKilled

working set 是 Linux 判斷要不要 OOM 的記憶體用量。碰到自己的 memory limit 會被 OOMKilled，只影響他自己的 Pod。

## 限制與沒資料時的處理

| 狀況 | 原因與處理 |
|---|---|
| 資料來源下拉選單是空的 | 沒有名稱符合 `Prometheus.*OCP` 的資料來源（大小寫有區分），修改 datasource 變數的 `regex` |
| PSI 面板沒資料 | Node 核心沒開 PSI，改看替代面板。要開啟需由平台團隊用 MachineConfig 加上 `psi=1` 核心參數，**套用時該 Node pool 會逐台重開機** |
| 磁碟寫入、磁碟讀寫，或磁碟忙碌程度「他（估算）」的線沒資料 | 查詢會依序嘗試三種 Pod 層級的磁碟指標（見下方）。三種都沒有，代表叢集沒有收集 Pod 層級的磁碟 IO，只能看整台 Node 的磁碟面板，用時間點對照 |
| 3C、3E 的虛線（壓測前 7 天峰值）沒資料 | 查詢用到 PromQL 的 `@ start()`。Prometheus 2.33 以後預設支援；資料來源若是舊版 Prometheus 或未開啟此功能的 Thanos，虛線會沒有資料 |
| 3C、3E 載入較慢 | 虛線要掃 7 天的資料，只算他的 Pod 和所選的 Node，通常可以接受 |
| 所有【整台 Node】面板都沒資料 | 這些查詢假設 node-exporter 的 `instance` 標籤是 Node 名稱（OCP 預設如此）。如果你的環境是 `IP:9100`，要修改查詢 |
| 網路相關面板沒資料 | 網卡名稱不是 `ens / eth / eno / enp / bond` 開頭，修改 `gen_dashboard.py` 的 `NIC` |
| 3B、3D 的網路 ÷ 網卡上限沒資料 | Node 沒有回報網卡速度，改看第 4 區的網路吞吐與丟包 |
| 磁碟相關面板沒資料 | 磁碟名稱不是 `sd* / nvme* / vd* / xvd*`，修改 `gen_dashboard.py` 的 `DEV` |
| 找不到 Deployment 的 Pod | 用 Pod 名稱規則 `<deployment>-<hash>-<id>` 對應，StatefulSet、DaemonSet 不適用 |
| 3A 的網路 ÷ Node 目前網卡流量超過 100% | 同一台 Node 內 Pod 互傳不經過實體網卡，只當量級參考 |
| 他的 CPU、記憶體看起來偏高 | 有些叢集會從 kubelet 兩個地方（`/metrics/cadvisor`、`/metrics/resource`）抓同一個容器指標，加總後變兩倍。所有容器指標查詢都已先「同一個容器只取一筆」再加總，所以不會重複計算。仍覺得偏高時，用 `oc adm top pod -n <namespace>` 的數字對照：兩邊接近，就是真的用這麼多 |
| 磁碟數字對不太上 | 容器層（cAdvisor）與 Node 層的統計方式不同，寫到 PVC 的量也不算在 Node 本機磁碟，只當量級參考 |
| Router 面板沒資料 | 服務不經過 Router，或沒有權限查詢 Router 指標 |

### 確認叢集有哪些 Pod 層級的磁碟指標

在 Grafana Explore 分別執行下面三個查詢，有回傳數字就代表該指標有收集。磁碟面板會依這個順序使用第一個有資料的：

```promql
count(container_fs_writes_bytes_total{container!="", container!="POD"})
```

```promql
count(container_fs_writes_bytes_total{container="", pod!=""})
```

```promql
count(container_blkio_device_usage_total{operation="Write", container="", pod!=""})
```
