# RIR Localization Preparation Report

生成日期：2026-08-13（Asia/Shanghai）

## 1. 真实数据审计

1. 实际找到 **22 个 continuous session**，另有 1 个旧版 single 结果目录（不混入数据集）。
2. continuous measurement 总数：**49**。
3. PASS：**6**。
4. FAIL：**43**。
5. 可训练格式样本：**6**（只足够 smoke test，不足以正式训练）。
6. 原始 `rir_float32.npy` shape：**49/49 均为 `[24000, 8]` = `[samples, channels]`**；
   内部统一为 `[8,24000]`。
7. dtype：**49/49 均为 `float32`**。
8. sample rate：**48,000 Hz**。
9. duration：**500 ms / 24,000 samples**。
10. PASS 可用样本中 CH0–CH6 均为有效非零通道。
11. CH7：**49/49 数组严格全零**；PASS 的 6/6 也全零，判定为 `inactive_zero`，默认配置排除。
12. Rx 坐标字段：`rx_pose.position_m`；3D、米，2D baseline 使用 x/y。
13. Tx 命名字段：`tx_pose.position_m`；源码实际语义是 Linux 本机 t1 时刻的 speaker pose。
14. 坐标 frame：当前可用样本为 `camera_init/user_zero`；orientation 为 `orientation_xyzw`。
15. ToF 字段：`tof.tof_seconds`；兼容字段 `exact_tof`，派生距离 `tof.distance_m`。
16. ToF 单位：秒；距离单位米。当前 metadata 标记 `preliminary / uncalibrated`。
17. RIR sample 0：检测到的 C2 arrival 前 10 ms；直达参考为 `direct_arrival_index=480`。
18. RIR 是否保留 absolute ToF：**否**。波形以 C2 arrival 做相对对齐；绝对 `t4_sample` 和独立
    `tof.tof_seconds` 留在 `result.json`。

实际观测的状态字段为 `quality.overall`，当前取值只有 `PASS` / `FAIL`；失败原因列表为
`failure_reasons`。49/49 个 `result.json.measurement_id` 均与六位目录编号一致，NPY 与 metadata
同目录原子绑定。measurement 时间戳为带 UTC offset 的 ISO-8601 `wall_clock_timestamp`，session
为 `start_timestamp` / `end_timestamp`。

Rx 标签在 `_finalize()` 中使用 C2 的 `t4_sample`，先映射到本机 `CLOCK_MONOTONIC`，再对 pose
timeline 插值得到 `microphone_pose`。当前 audio mapping 标记 `hardware_timestamp=false`，仍包含
callback/ALSA delivery latency 的未标定误差。

## 2. 一致性与空间 Gate

- 9 条同时有 named `tx_pose`、`rx_pose`、ToF 的记录中，字段几何距离均值约 **0.00287 m**，
  ToF 距离均值约 **16.5033 m**，`ToF - geometry` 均值约 **16.5004 m**。
- 这不只是数值异常：initiator 采集的目标 RIR 来自远端 Android C2，而顶层 `tx_pose` 是本机
  C1 speaker at t1。Android reply 中的 `android_position_*_m` 当前又属于另一个 `manual_map`
  frame，因此不能与 Rx 的 `camera_init/user_zero` 直接比较。
- 6 个可用 Rx 点的 x 范围 `[-0.00299, 0.01137] m`，y 范围
  `[-0.000734, 0.01884] m`，bounding-box 面积约 **0.000281 m²**。
- 相邻步距 mean `0.00611 m`、median `0.00255 m`、P90 `0.01339 m`；5 cm 阈值下
  near-duplicate ratio 为 **100%**。

结论：当前数据可以验证软件，但不能用于有意义的正式定位训练或性能报告。

## 3. 实现文件

19. 新增：`__init__.py`、`utils.py`、`inspect_dataset.py`、`build_dataset.py`、
    `split_dataset.py`、`dataset.py`、`model.py`、`checkpoint.py`、`train.py`、`evaluate.py`、
    `infer.py`、`baselines.py`、`config.yaml`、`requirements.txt`、`README.md` 和本报告。
20. 是否修改现有采集代码：**否**。
21. 原因：每轮 Rx pose 已明确绑定在 `result.json`，不需要为 baseline 改动工作正常的采集核心。
    远端 Tx 同 frame 绑定是正式采集前的新 metadata 需求，应做独立、最小且经过确认的改动。

实现能力包括：递归审计、NPY 优先/WAV fallback、FAIL 排除追踪、三种 split、任意通道、
`[T,C]`/`[C,T]` 自动识别、crop/pad、none/peak/rms normalization、train-only 坐标标准化、
RIR-only / RIR+ToF、回归/网格分类、AdamW/AMP/clip/scheduler/early stop/resume、米制评估、
checkpoint 自包含预处理、单条推理和低维特征 KNN。

## 4. Smoke test

最新 smoke run：`runs/20260813_185343_rir_regression_baseline_SMOKE`

22. Dataset smoke test：**PASS**，单样本 RIR `[7,24000]`，target `[2]`。
23. DataLoader smoke test：**PASS**，batch RIR `[4,7,24000]`，target `[4,2]`。
24. CNN forward：**PASS**，模型参数量 309,346。
25. loss / backward / AdamW optimizer step：**PASS**。
26. best/last checkpoint save + reload：**PASS**。
27. `evaluate.py` 独立执行与 CDF/scatter/histogram/spatial map/predictions/metrics：**PASS**。
28. `infer.py` 的 measurement 模式和直接 NPY 模式：**PASS**。
29. 全部工程 smoke 项：**PASS**。Classification forward/cross-entropy/grid decode 和 KNN 也通过。

Smoke test 只有 4 train / 1 val / 1 test 样本。任何毫米级输出误差都只是极小、近重复数据上的
链路产物，**不得解释为模型最终定位精度**。

## 5. 正式训练前问题

30. 必须先解决：

    - 把固定远端 Tx 坐标写入与 Rx 相同的 world frame，并明确其采样/冻结时刻与设备外参；
    - 重新采集覆盖真实房间二维面积的足量 PASS 样本，降低近重复率并预留空间/跨 session test；
    - 校准或诊断当前 45–53 ms preliminary ToF；
    - 新采集后重新运行 inspect/build/split，重新确认 CH7、shape、frame、时间参考和 gate；
    - 由用户确认数据 Gate 后才运行不带 `--smoke-test` 的正式训练。

## NOT READY FOR FORMAL TRAINING

原因是监督数据空间覆盖不足、样本量不足、远端 Tx 不在统一坐标系、ToF 尚未标定；不是工程
链路故障。当前任务已按要求停止，没有执行正式长时间训练。
