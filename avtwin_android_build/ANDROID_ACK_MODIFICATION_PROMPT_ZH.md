# Android UDP ACK 修改提示词

下面内容可以直接复制给负责另一个 Android 工程的开发者或代码助手：

```text
请修改我的 Android/Kotlin 声学 Responder，使它兼容 AV-Twin Linux 的可靠 UDP 控制协议。

网络方向：
- Android 在 controlPort（默认 5006）持续监听 Linux。
- Linux 在 resultPort（默认 5005）监听 Android。
- UDP 只用于控制、确认和传递测量元数据，绝不能作为 t2/t3/ToF 的时间来源。

1. ARM 接收与确认
Linux 发送：
{"type":"arm","protocol_version":1,"session_id":"S","measurement_id":M,"arm_event_id":"A"}

Android 收到后必须校验来源 IP、protocol_version、session_id、measurement_id 和 arm_event_id，
并从接收 ARM 的同一个 DatagramSocket 原路回复：
{"type":"arm_ack","protocol_version":1,"session_id":"S","measurement_id":M,"arm_event_id":"A","accepted":true,"reason":"accepted_strict","receiver":"android"}

首次有效 ARM 将 Android 本次运行绑定到 Linux session_id=S；之后不同 session_id 必须回复
accepted=false, reason=session_id_mismatch。相同 arm_event_id 的重发必须幂等：再次返回
accepted=true, reason=duplicate_arm_reack，但不能再次改变检测代次、不能重复触发 C2。
同一 measurement_id 但新的 arm_event_id 必须拒绝。

2. reply_timing 与 Linux 确认
Android 在声学检测 C1、播放 C2 并获得 PCM/AudioTimestamp 时序后发送：
{"type":"reply_timing","protocol_version":1,"session_id":"S","measurement_id":M,"android_event_id":"E",...原有t2/t3字段...}

一次测量只能生成一个稳定的 android_event_id=E。发送后等待最多 350 ms，Linux 会返回：
{"type":"reply_ack","protocol_version":1,"session_id":"S","measurement_id":M,"android_event_id":"E","accepted":true,"reason":"accepted","receiver":"linux"}

没有收到匹配 ACK 时，使用完全相同的 JSON 和 event_id 最多发送 3 次。收到匹配且
accepted=true 的 reply_ack 后立即停止重发。不同 session/measurement/event 的 ACK 不得
结束等待。三次均无 ACK 时记录 REPLY_ACK_TIMEOUT，但不得再次播放 C2。

3. 并发与安全
- UDP 监听线程不得阻塞 AudioRecord 实时线程。
- C1 -> C2 的立即响应路径中，先把 C2 提交给预加载 AudioTrack，再做 JSON、日志和 UDP。
- 所有重复包按 event_id 去重；ACK 可以重复，声学动作不能重复。
- ACK 的 System.nanoTime/网络 RTT 只记录诊断，不参与 t1/t2/t3/t4、reply_delay 或 ToF。
- 日志至少输出 ARM_ACK_SENT、ARM_REJECTED(reason)、REPLY_TIMING_SENT(attempt)、
  REPLY_ACK_RECEIVED、REPLY_ACK_TIMEOUT。

4. 验收测试
- ARM ACK 丢失：Linux 重发同一 A，Android 再 ACK，但只保留一个 pending ARM。
- 不同 session：明确拒绝且不允许 C1 触发 C2。
- reply_ack 丢失：Android 最多发送 3 个完全相同的 E，C2 只播放一次。
- 迟到/重复 reply_ack：不影响下一 measurement。
- 所有 ACK 正常时：Linux 收到 arm_ack 后才播放 C1，Android收到 reply_ack 后停止重发。
```
