# AV-Twin Android Responder

Temporary Android responder used to reproduce the AV-Twin two-way acoustic handshake.

## v0.9 UDP ACK protocol

- The first valid Linux ARM binds the formal protocol `session_id`; the Android storage
  session remains a separate local identifier.
- Every ARM is answered with `arm_ack`. Repeated packets carrying the same `arm_event_id`
  are idempotently re-ACKed and never arm C2 twice.
- `reply_timing` carries one stable `android_event_id` and is sent up to three times until
  Linux returns `reply_ack`.
- ACK timestamps are diagnostics only and never enter acoustic t1/t2/t3/t4 or ToF.

## v0.8.3 manual pose capture

- Enter X/Y/Z in metres and yaw/pitch/roll in degrees, then apply the pose.
- The applied pose remains editable while a STRICT ARM session is running.
- Every accepted C1 freezes one pose snapshot for that `measurement_id` before C2 playback.
- Repeated rounds are appended to `manual_pose_records.csv` and `events.jsonl`.
- The same pose fields and an xyzw quaternion are included in the `reply_timing` UDP message.

## v0.2
- 48 kHz mono PCM16 recording
- C1: 11-19 kHz, 0.2 s
- C2: 300 Hz-9 kHz, 0.2 s
- AudioTrack uses MODE_STREAM for broader tablet compatibility, including Xiaomi Pad 7S Pro / HyperOS devices where MODE_STATIC may fail to initialize.
- Detect C1 -> record t2 -> play C2 -> self-detect t3 -> report t2/t3 to Linux via UDP.

## UDP 双向检验

界面的 `UDP 双向检验` 不播放声音。Android 向配置的 Linux 结果端口发送
`AVTWIN_UDP_TEST_V1 / udp_test_ping`，只有在 2 秒内收到相同 `nonce` 的
`udp_test_reply` 才显示 PASS，并报告 RTT 和回包来源。因此 PASS 同时证明
Android → Linux 和 Linux → Android 两个方向可达。

声学会话运行时，Android 控制端口也会响应 Linux 发起的同一 ping。回复始终发送到数据报的
源 IP 和源端口；STRICT ARM 的 Linux 来源地址限制同样适用于测试请求。

从 v0.9.2 开始，Android 界面打开且未运行声学会话时也会在控制端口提供独立测试监听，因此
Linux 可以直接点击 `Test UDP Roundtrip`。开始声学会话前该监听会释放端口，再由正式
STRICT ARM 监听接管。Android 的“Linux 电脑 Wi-Fi IPv4”必须填写 Linux 实际 Wi-Fi 地址，
不能填写雷达专用有线口地址。
