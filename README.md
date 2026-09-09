# 机器人主控流式转发夹爪 OTA 工具

现在同时提供图形界面和命令行。日常升级直接双击本目录的
`启动夹爪以太网OTA.pyw`，即可在窗口中选择 BIN、夹爪编号和版本号。

本工具实现以下链路：

```text
本工具（TCP Server，端口5001）
  → 网线 / JSON
机器人主控 STM32H723（TCP Client）
  → UART7 或 UART8 / RS485 Bootloader二进制协议
夹爪 STM32G431 Bootloader
  → 写入0x08006000 Application区并启动新程序
```

机器人主控只保存当前收到的 672 字节数据块。夹爪确认该块已经写入内部
Flash 后，主控才向本工具回复 `gripper_ota_ack`；主控不会把完整夹爪固件
保存到自己的 Flash。

## 1. 使用前提

1. 机器人主控烧录了包含 `gripper_ota_manager` 和
   `gripper_boot_transport` 的新固件。
   本轮回包修复还包含 `ota_net_reply.c`；必须重新编译并烧录H723 Application，
   连接日志中的转发固件标识应为 `20260908-net2`，具体检查见5.2节。
2. 目标夹爪已经烧录 `Bootloader_G431`。
3. 夹爪当前 Application 支持 `0x00F0/0x00F1/0x00F2` 三个 OTA
   切换寄存器。夹爪1当前工程已支持；夹爪2必须完成同样适配后才能从
   Application 自动进入 Bootloader。
4. 待升级 BIN 必须按 `0x08006000` 链接，并包含 OTA 身份签名。
5. 电脑有线网卡地址应为 `192.168.0.20`，因为机器人当前会主动连接
   `192.168.0.20:5001`。如需改地址，应修改机器人
   `Device/Net/tcpclient.h` 中的 `DEST_IP_ADDR0~3` 并重新编译主控固件。
6. Windows 防火墙允许 Python 监听 TCP 5001，且同一时刻没有其他程序
   占用该端口。

工具只使用 Python 标准库，不需要安装第三方包。使用 Python 3.10 或更高版本，
图形界面需要安装 Python 时附带的 Tcl/Tk（Tkinter）。

## 2. 图形界面与操作

### 2.1 打开窗口

双击 `启动夹爪以太网OTA.pyw`。如果 Windows 没有关联 `.pyw` 文件，
在本目录打开 PowerShell 执行：

```powershell
python .\gripper_ota_tcp_tool.py
```

不带参数时默认打开图形界面；也可以运行 `python .\gripper_ota_tcp_gui.py`。
如果提示缺少 `_tkinter`，通过 Python 安装程序的 Modify 安装 Tcl/Tk 支持。

### 2.2 每个选项和按钮

| 控件 | 含义与使用方法 |
|---|---|
| 本机监听地址 | 默认 `0.0.0.0`，表示监听电脑所有网卡；也可填电脑有线网卡地址 `192.168.0.20`，不要填机器人 IP |
| 端口 | 默认 `5001`，必须和主控配置一致 |
| 开始监听 | 打开电脑 TCP Server，等待机器人主动连接；此操作不会开始升级 |
| 停止监听 / 断开 | 关闭当前连接和监听端口；升级期间禁用，先结束或取消升级 |
| 连接状态 | 区分“未监听”“等待机器人连接”“已连接”和“连接已断开，等待重连” |
| BIN 路径 / 选择 BIN | 选择或粘贴夹爪 Application BIN 路径；选文件后显示大小和 CRC32，开始前再次检查文件 |
| 夹爪编号下拉框 | `1`=UART7/从站1，`2`=UART8/从站2；同时确定固件 role，没有独立 role 或从站 ID 选项 |
| Major / Minor / Patch | 三个版本分量，取值均为 `0～255`；必须与 BIN 内版本一致。当前仓库夹爪1为 `1.15.0`。填写这些数值不会修改 BIN 内部的版本 |
| 开始 OTA 升级 | 先查询主控夹爪OTA接口，确认回复后才请求进入维护态和 Bootloader，再传输、校验、复位并确认新程序启动 |
| 查询升级状态 | 查询主控当前会话，显示 IDLE、STREAMING、RECOVERY_REQUIRED 等；查询的是主控全局 OTA 会话，不会让下拉框所选夹爪进入 Bootloader |
| 取消升级 | 升级准备或数据传输期间请求取消，并等待主控确认；开始整包校验后禁用，等待校验和启动结果 |
| 取消当前会话 | 空闲且已连接时显示；先查询主控当前会话 ID，再取消该会话，用于处理上一次中断遗留的会话 |
| 阶段提示 | 显示准备、传输、校验、启动确认或恢复要求；成功以新 Application 确认运行为准 |
| 进度条 / 字节数 | 按主控 ACK 中夹爪已确认写入的字节数更新；100% 后仍要等待校验及启动确认 |
| 日志 / 清空日志 | 显示连接、固件、阶段、重试和错误；清空只影响窗口显示，不改变升级会话 |

图形界面不提供硬件版本和 ST-Link 数据包按钮，manifest 硬件版本沿用默认值0。
当前主控流程固定为自动进入 Bootloader、成功后自动复位并检查 Application，
因此没有提供会改变这两个步骤的复选框。

### 2.3 正常操作顺序

1. 电脑通过网线连接机器人，电脑有线网卡设为 `192.168.0.20`，关闭其他占用
   TCP 5001 的上位机程序。
2. 打开本工具，保留监听地址 `0.0.0.0` 和端口 `5001`，点击“开始监听”。
3. 等待状态显示“已连接”及机器人地址；只显示“等待机器人连接”时尚不能升级。
4. 点击“选择 BIN”，选择夹爪固件，例如
   `gripper-program/OTA_PC_Tool/gripper1_app.bin`。
5. 下拉选择“夹爪编号 1”，当前仓库固件填写 `Major=1 / Minor=15 / Patch=0`。
6. 点击“开始 OTA 升级”，先观察“0/4 主控接口检查”是否回复状态，再进入准备与传输。
   传输时文件、夹爪、版本和连接按钮锁定，
   窗口仍可响应取消操作。
7. 等待“升级成功：夹爪 1 / 1.15.0 已运行”提示。连接会保留，可再次查询或升级。

选择夹爪2前，必须先完成夹爪2的 Bootloader 和 Application 适配；仅在界面选择2
不能替代适配，也不能把夹爪1的 BIN 改造成夹爪2固件。

取消后的 `recovery_required=true` 表示需要重新发送完整 BIN，主控会保持维护态。
如果断网，窗口继续监听并显示等待重连；重连后先查询状态，再按第4节恢复，
不会自动从当前进度续传。空闲时后台持续接收主控的普通状态报文，避免它们堆积。

关闭窗口时，如果还在准备或传输，会先请求取消并等待响应；如果已经在校验或
启动确认阶段，会等当前操作结束再关闭。取消响应超时会记录在日志中，不能把
窗口已关闭理解为夹爪应用已经恢复。

### 2.4 保留命令行用法

在本目录打开 PowerShell：

```powershell
python .\gripper_ota_tcp_tool.py --bin "D:\固件\gripper1_app.bin" --gripper 1 --version 1.15.1
```

工具先监听端口，机器人主控随后主动连接。不要把机器人地址填到 `--host`；
默认 `0.0.0.0` 表示监听电脑的所有网卡。

常用参数：

| 参数 | 含义 | 默认值 |
|---|---|---:|
| `--gui` | 打开图形界面；不带任何参数时也是此行为 | — |
| `--action` | `update`、`query` 或 `cancel` | `update` |
| `--bin` | 夹爪 Application BIN | update 时必填 |
| `--gripper` | `1`=UART7/从站1，`2`=UART8/从站2；同时作为固件 role | `1` |
| `--version` | 目标 `Major.Minor.Patch`，必须和编译进 BIN 的版本一致 | update 时必填 |
| `--hardware-revision` | 写入 manifest 的硬件版本，当前原型一般使用0 | `0` |
| `--host` | 本机监听地址 | `0.0.0.0` |
| `--port` | TCP监听端口 | `5001` |
| `--accept-timeout` | 等待机器人连接的秒数 | `120` |
| `--ready-timeout` | 等待维护态、HELLO和BEGIN完成的秒数 | `30` |
| `--ack-timeout` | 单个672字节网络块的ACK等待秒数 | `12` |
| `--end-timeout` | 等待夹爪整包校验结果的秒数 | `20` |
| `--boot-timeout` | 等待新Application版本/READY实读结果的秒数 | `20` |
| `--retries` | 网络DATA块超时后的重发次数 | `3` |
| `--session-id` | cancel 时指定原会话 ID；省略则先查询当前会话 | `0` |

`--gripper` 与固件 role 是同一个物理身份，因此工具没有再提供容易选错的
独立 role 参数。

## 3. 正常流程与成功判据

工具依次执行：

1. 本地检查 BIN 大小、MSP、Reset_Handler、链接地址和 OTA 身份签名，并
   计算 CRC-32/ISO-HDLC。
   随后发送 `gripper_ota_query`，每次等待5秒，最多两次；只有主控回复IDLE，
   或同一夹爪的RECOVERY_REQUIRED，才继续发送START。检查失败时不发送START或CANCEL。
2. 发送 `gripper_ota_start`。主控进入全机器人维护态、失能两个夹爪，
   通过 Modbus 让目标进入 Bootloader，校验 HELLO 后执行 BEGIN 擦除。
3. 收到 `gripper_ota_ready` 后，以 672 字节一块发送 DATA；主控再拆为
   192、192、192、96 字节（末块按实际长度）发送到夹爪。
4. 每收到一个 `gripper_ota_ack`，其 `committed_offset` 都表示夹爪内部
   Flash 已确认到该位置。ACK 超时时工具重发完全相同的块，不会推进序号。
5. 发送 END，先等待 `gripper_ota_result`，表示夹爪向量表、整包 CRC 和
   VALID Metadata 已提交。
6. 主控命令夹爪复位，再通过普通 Modbus 实读 `DEV_ID`、固件版本、
   Bootloader版本、Application READY、布局版本和 role。
7. 只有读值与目标完全一致，主控才返回 `gripper_ota_boot_ok` 并退出维护态。

屏幕出现“Application x.y.z 已运行”才是完整成功；仅看到数据发送100%或
`gripper_ota_result`，还不能证明新 Application 已经启动。

## 4. 查询与取消

查询当前主控 OTA 会话及夹爪 Bootloader 持久化进度：

```powershell
python .\gripper_ota_tcp_tool.py --action query
```

取消当前会话：

```powershell
python .\gripper_ota_tcp_tool.py --action cancel
```

如果 BEGIN 尚未擦除 Application，主控会尝试让旧 Application 恢复并退出
维护态。如果已经收到 READY，说明 BEGIN 和擦除已经完成；此时取消只会终止
下载，返回 `recovery_required=true`，机器人会继续保持维护态。必须重新运行
update 并发送完整 BIN，不能恢复普通夹爪控制或反复断电碰运气。

活动会话连续60秒未收到任何夹爪OTA命令时，主控会进入
`RECOVERY_REQUIRED`，继续保持维护态而不会自动使能执行机构。重新连接后先
执行 query，再重新运行完整 update。

## 5. 错误信息

`gripper_ota_error` 的重要字段：

| 字段 | 含义 |
|---|---|
| `stage` | 失败阶段，如 `ENTER_BOOT`、`BEGIN`、`DATA_RS485`、`END_VERIFY` |
| `downstream_status` | 夹爪 Bootloader 原始状态码 |
| `downstream_status_name` | 原始状态名，如 `WRONG_ROLE`、`BAD_OFFSET`、`IMAGE_CRC_FAILED` |
| `committed_offset` | 主控已向上游确认的字节数 |
| `recovery_required` | 是否必须保留维护态并重新发送完整镜像 |

常见定位：

- `gripper bus is not initialized`：主控的夹爪任务尚未初始化完成，稍后重试。
- `no Bootloader HELLO response`：检查主控到目标夹爪的UART映射、A/B线、共地、
  波特率115200，以及夹爪Application是否包含OTA切换寄存器。
- `WRONG_ROLE`：Bootloader锁定角色与 `--gripper` 不一致。
- `DATA_RS485`：检查485干扰、电源稳定性和夹爪是否意外复位；先执行 query。
- `WAIT_APPLICATION`：固件虽已写入，但新程序未正常运行，或 BIN 内版本号与
  `--version` 不一致。保持维护态并使用 ST-Link/直连485进一步恢复。

### 5.1 TCP已连接，但READY和CANCEL均无回复

2026-09-08实机截图记录：BIN 46996字节，CRC32 `0x39E48A9F`，进度0%；
等待 `gripper_ota_ready` 30秒超时，随后等待取消回复也超时。
这表示上位机没有收到准备完成确认，不能由此确定主控是否已执行START、夹爪
是否已进入Bootloader或开始擦除，也不能断定是BIN大小、夹爪跳转或RS485线路问题。

当前代码中，进入维护态、HELLO和BEGIN失败均有 `gripper_ota_error` 回复路径。
应先验证主控的命令/回复链路。新工具增加了：

- `[TX]`/`[RX]` 控制报文日志；普通遥测只按首次出现的topic记录，避免刷屏；
- 升级前自动执行状态查询；TCP连接本身不再作为OTA接口可用的判断依据；
- 超时区分“未收到TCP数据”“有字节但没有完整JSON”“收到了JSON但缺少目标回复”，
  并统计会话/序号不匹配的目标回复数。

更新工具后先点击“查询升级状态”，根据结果继续：

| 结果 | 可以确定什么 / 下一步 |
|---|---|
| 回复 `IDLE` | 主控QUERY路由和回包链路可用。空闲QUERY不访问夹爪RS485，可继续升级以定位START阶段 |
| 回复 `RECOVERY_REQUIRED` | 主控已记录恢复会话；针对回复中的同一夹爪重发完整BIN |
| 回复 `STREAMING` 等 | 原会话仍存在，先取消该会话，再重新完整升级 |
| 只收到普通遥测 | 主控到电脑方向有数据；检查主控实际运行槽位、网络命令接收/分发和OTA回复是否被丢弃 |
| 完全没有TCP数据 | TCP握手不等于应用任务正常，检查Network/NetCmd任务、发送开关和调试器是否暂停CPU |
| 有字节但没有完整JSON | 检查实际报文格式或截断情况，不先归因到夹爪 |

若QUERY也超时，可用现有Keil变量继续定位，无需先改夹爪Bootloader：

1. 核对主控实际运行槽位与下载目标一致；工程的Slot A偏移为 `0x20000`，
   Slot B偏移为 `0x80000`。已下载某槽不直接证明复位后Bootloader选择了该槽。
2. 记录一次查询前后的 `g_cmd_recv_count`、`g_cmd_parse_count`、
   `g_dbg_last_topic`、`g_dbg_topic_nomatch`。
3. 收到查询后，`g_dbg_last_topic` 应是 `gripper_ota_query`；接收计数不增加时
   检查 `RxQueueHandle`、`NetCmd_myTaskHandle` 和网络接收回调。
4. 接收计数增加、解析计数不增加时，在 `GripperOTA_HandleQuery` 定位是否进入/返回；
   解析计数增加而电脑没收到回复时，旧版本检查普通发送池/队列；
   `20260908-net2` 版本检查独立控制队列及 `robot_ota_link` 中的
   `reply_full`、`tx_sent`、`tx_failed`，普通 `g_tcp_send_*` 不统计该队列。
5. 特别核对现场构建的 `ENABLE_DATA_UPLOAD`。当前仓库值为1，但旧发送循环在
   值为0时会把包括OTA回复在内的TxQueue消息丢弃，表现也可能是TCP连接正常而所有
   回复超时。新版本的夹爪OTA控制队列已在此开关之外发送；不要通过关闭
   正常遥测来替代修复。本次未改机器人自身OTA的普通TxQueue路径。

以上是前一轮诊断；本轮主控修复及部署流程如下。夹爪启动逻辑仍未改动。

### 5.2 本轮回包修复：20260908-net2（2026-09-09收尾验证）

最新截图显示：查询等待5秒期间收到6366条普通JSON，但没有状态回复。
这证明主控→电脑方向有业务数据；此时工具尚未发送START，没有通过本次操作
开始擦除夹爪。不能据此断定RS485坏了，也不能证明电脑→主控命令已被处理。

代码检查确认旧实现存在缺陷：夹爪OTA回复与高频遥测共用发送缓冲池/队列，
池互斥锁争用、池满或队列满时可直接丢弃回复，调用者没有处理失败。
这是已修复的代码风险，但是否为截图中唯一实机根因，仍需新日志确认。

本轮改动：

- 为夹爪OTA控制回复保留8×512字节静态队列，不占用普通遥测池或新增RTOS堆；
  Network任务优先发送，仍由同一任务操作TCP。CMSIS-FreeRTOS忽略消息优先级，
  因此不能仅提高普通队列的 `msg_prio`。
- 关键回复入队最多等待约100ms；失败则通知Network断开连接，发送失败也断开，
  避免静默丢包或在可能只发了一部分的JSON后直接拼接重试。重新连接后先查询状态，
  不自动继续写入，不把断线当成取消或成功。
- 收到夹爪OTA命令后暂停生成普通遥测2秒，维护态中的原暂停机制保持不变；
  不改变电机控制任务，也不绕过升级前失能检查。
- 主控主动报告运行槽位/固件标识、查询接收回执、准备阶段；上位机显示这些信息。
- 夹爪会话超时检查改由NetCmd串行执行，避免小栈Monitor任务生成回复及并发修改会话。
- 弹窗只显示精简错误，完整报文保留在日志；弹窗出现前先恢复界面按钮。

#### 重新部署和验证

1. 关闭旧上位机进程，将本目录完整复制到实际运行工具的电脑，至少同时更新
   `gripper_ota_tcp_tool.py` 和 `gripper_ota_tcp_gui.py`。Linux也必须替换文件并重启，
   只修改Windows工作区不会更新Linux上正在运行的副本。
2. 打开 `robot-program_0827/MDK-ARM/STM32H723VGT6.uvprojx`，选择与实际运行槽一致的
   `Robot_App_Slot_A` 或 `Robot_App_Slot_B`，编译并按已验证的Keil设置下载。
   两个Target已添加 `Device/Net/ota_net_reply.c`，不需要手工再加一次。
   A/B分别链接到 `0x08020000` / `0x08080000`；不要把A镜像烧到B地址。
   保持分区擦除，勿为本次测试整片擦除或覆盖Bootloader/配置区。
3. 本轮不需要重新烧录H723 Bootloader，也不需要重新烧录G431 Bootloader/Application。
4. 打开新工具，开始监听。连接日志应出现类似：

   ```text
   主控实际运行 Slot A，转发固件标识 20260908-net2，NetCmd已创建=True
   [RX] {"topic":"robot_ota_link", ...}
   ```

   Slot B也正常，但必须与选择的固件对应。没有标识时先核对实际槽位和文件副本，
   不把“Keil下载成功”直接当成“复位后正在运行该镜像”。
5. 先点“查询升级状态”。正常空闲返回顺序是 `gripper_ota_rx`（`accepted:true`、
   `reason:"queued"`），随后 `gripper_ota_status`（`state:"IDLE"`）。
   `accepted:true`只表示投递命令队列成功，不表示夹爪已进入Bootloader。
6. 再选择夹爪1和BIN，核对版本后开始升级。日志应依次显示阶段
   `MAINTENANCE`、`ENTER_BOOT`、`BEGIN`，然后READY、块ACK、RESULT和BOOT_OK。
   `BEGIN`阶段通知是在尝试擦除前发出，只有READY表示BEGIN已成功确认。
7. 若失败，保留从连接标识到错误的完整日志。维护态或恢复状态下不得直接恢复运动，
   按查询的 `recovery_required` 处理。暂不使用尚未完成适配的夹爪2测试。

#### 新日志定位表

| 最后收到的信息 | 说明 / 检查方向 |
|---|---|
| 只有普通遥测，没有 `robot_ota_link` | 尚未确认新版本在运行，核对固件槽位、烧录产物和PC工具副本；也可能控制回包链路仍异常 |
| 有固件标识，无查询接收回执 | 检查主控TCP接收、JSON分帧和接收回调；不能只据此确定网线故障 |
| `netcmd_created:false` | 命令任务创建失败，检查FreeRTOS堆余量和任务创建返回值 |
| `accepted:false` | 主控已收到命令，但接收池/队列拒绝；`reason`区分池忙/满、队列满和帧过长 |
| `accepted:true`，无状态回复 | 已进入RxQueue，检查NetCmd调度、正在处理的命令及回复路径 |
| 状态IDLE后，停在某个 `gripper_ota_stage` | QUERY链路已通，再按MAINTENANCE/ENTER_BOOT/BEGIN定位维护态或下游通信 |

`netcmd_alive`表示任务曾进入过循环，不是实时健康保证；`cmd_age_ms`是回执产生时
距离上次循环心跳的时间。START执行耗时操作时变大可以是正常现象。
`cmd_seen/cmd_done`统计所有网络命令，不仅是OTA；回执在命令处理前产生，单条回执
里的旧计数不能直接判定任务卡死。`reply_full/tx_failed`是累计计数，需比较变化。

## 6. 开发自检

```powershell
python -m unittest -v .\test_gripper_ota_tcp_tool.py
```

2026-09-09 共17项测试通过，覆盖当前夹爪 BIN 的布局/CRC、1 KiB接收限制、
TCP粘包/拆包、ACK丢失重传与迟到ACK、进度回调、取消、缺少Application READY
时禁止报成功，以及本机回环TCP模拟完整升级后继续查询。界面测试创建隐藏的
Tk窗口，检查夹爪/文件选择传参和升级期间按钮锁定、结束后恢复。
新增测试确认主控不回复QUERY时不发送START/CANCEL、超时区分无数据与普通遥测，
并确认诊断日志不会随周期遥测反复刷屏。本轮还覆盖6366条遥测夹杂查询回执及
状态回复、接收拒绝提示、固件/任务诊断分层、新查询清除旧回执，以及错误弹窗
出现前按钮恢复和长日志截短。

这些测试是PC侧模拟/回环和界面测试，不是主控RTOS队列压力测试，也不连接真实机器人。
H723两个Target另行经过Keil编译检查；本轮必须更新H723 Application和PC工具，
G431固件保持不变。完整硬件链路仍须按5.2节进行实机验证。
