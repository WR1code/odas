# RIR → Rx 二维坐标定位工程

本目录是在现有 AV-Twin C1/C2、ToF、RIR 和 `manual_continuous` 采集系统之外新增的独立模块。
它只读取原始采集目录，把索引、图表、split 和 run 写入新目录；没有重切连续 WAV，也没有修改
现有握手、UMA-8 streaming 或 RIR 生成代码。

当前阶段只完成正式训练前的数据审计和可复现工程链路。不要把任何 `*_SMOKE` run 的误差当作
模型性能；当前真实数据远不足以正式训练。

## 已核实的真实数据格式（2026-08-13）

对 `avtwin_linux/output` 的源代码和真实文件检查结果如下：

- 连续 session 22 个，measurement 49 条：`quality.overall` 为 `PASS` 6 条、`FAIL` 43 条。
- 另有 1 个旧版 single 目录；检查器会报告但不混入 continuous session 数据集。
- 状态字段为 `quality.overall`，布尔状态为 `quality.overall_pass`；失败原因列表为
  `failure_reasons`，质量 gate 详情在 `quality`。
- measurement ID 为顶层 `measurement_id`（整数），也对应六位目录名；session ID 为顶层
  `session_id` 和 `session.json.session_id`。
- measurement 时间戳字段为 `wall_clock_timestamp`，是带 UTC offset 的 ISO-8601；session 使用
  `start_timestamp` / `end_timestamp`。
- Rx 坐标为 `rx_pose.position_m`，Tx 命名字段为 `tx_pose.position_m`；均为三维米制坐标，朝向为
  `orientation_xyzw`，当前 frame 为 `camera_init/user_zero`。2D baseline 只取 Rx 的 x、y。
- `rx_pose` 是连续采集 `_finalize()` 中按声学 `t4_sample` 映射到 `CLOCK_MONOTONIC` 后插值的
  `microphone_pose`；`source_pose_timestamp_ns` 与本轮 RIR 严格写入同一个 `result.json`。
- `tx_pose` 的真实实现语义是本机 `t1` 时刻的 `speaker_pose`，并不是 C2 远端 Android Tx 的
  世界坐标。Android reply 中虽有 `android_position_*_m`，当前是另一个 `manual_map` frame，不能
  与 `camera_init/user_zero` 直接算几何距离。因此检查器按字段字面生成 ToF/geometry 图，但该图
  不能当成真正 Tx–Rx 几何验证；当前约 15–18 m 的 ToF 距离与毫米级字段距离巨大不一致。
- ToF 为 `tof.tof_seconds`（秒），派生距离为 `tof.distance_m`（米）；`exact_tof` 是兼容字段。
  当前 ToF 标记为 `preliminary / uncalibrated`。
- 所有 49 个 NPY 原始 shape 都是 `[24000, 8]`，即 `[samples, channels]`，dtype `float32`；
  采样率 48 kHz、时长 500 ms。Dataset 会自动转成 `[8,24000]`，DataLoader 输出 `[B,C,T]`。
- CH7 在 49/49 个数组中严格全零，检查结果为 `inactive_zero`。当前默认使用 CH0–CH6；这是由
  数据审计得出的配置，不是 Dataset 中硬编码删除。可把配置改成 `[0]` 做论文风格单通道实验。
- `rir.py` 从检测到的 C2 arrival 前 10 ms 开始估计 RIR：sample 0 是 C2 arrival 前 10 ms，
  `rir.direct_arrival_index=480` 是 0 ms 直达参考。RIR 已对直达路径做相对时间对齐，不再包含
  absolute ToF；绝对 `t4_sample` 和 `tof.tof_seconds` 仍保存在 metadata。预处理不会再次对齐。
- 当前 6 个可用点的 XY bounding-box 面积约 `2.81e-4 m²`，5 cm 阈值下 near-duplicate ratio
  为 100%。它们只够做 smoke test，不够训练或评价定位模型。

完整机器可读检查结果在 `artifacts/inspection_real/inspection_report.json`，构建统计在
`artifacts/dataset_real/dataset_stats.json`。

## 环境

项目已有 `.venv` 时可直接使用；否则：

```bash
cd /home/w/odas/odas
python3 -m venv .venv
source .venv/bin/activate
pip install -r rir_localization/requirements.txt
```

CPU 环境若 PyPI 的 `torch` 尝试下载 CUDA 依赖，可先安装官方 CPU wheel，再装其余依赖：

```bash
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r rir_localization/requirements.txt
```

以下命令均从仓库根目录 `/home/w/odas/odas` 执行。无显示服务器时可加
`MPLCONFIGDIR=.runtime/matplotlib`；绘图后端已固定为 `Agg`。

## 1. 数据检查

```bash
.venv/bin/python rir_localization/inspect_dataset.py \
  --input avtwin_linux/output \
  --output rir_localization/artifacts/inspection_real
```

输出包含 session/measurement、PASS/FAIL、pose、NPY、NaN/Inf/shape、RIR 通道统计、CH7 检测、
直达 sample、ToF、空间覆盖、步距与近重复率，以及：

- `dataset_spatial_coverage.png`
- `tof_vs_geometry_distance.png`
- `tof_geometry_error_histogram.png`
- `inspection_report.json`

## 2. 构建 dataset

```bash
.venv/bin/python rir_localization/build_dataset.py \
  --input avtwin_linux/output \
  --output rir_localization/artifacts/dataset_real
```

优先读取 `rir_float32.npy`，缺失时才尝试逐通道 WAV。不会复制原始 RIR。FAIL、缺 pose、坏 RIR、
NaN/Inf 或全通道无能量样本不会进入 `dataset.csv`，而会连同原始状态和原因写入
`excluded_samples.csv`。

## 3. 创建 split

Sequential（论文采集顺序风格）：

```bash
.venv/bin/python rir_localization/split_dataset.py \
  --dataset rir_localization/artifacts/dataset_real/dataset.csv \
  --output rir_localization/artifacts/splits_real \
  --mode sequential --train-ratio 0.7 --val-ratio 0.1 --test-ratio 0.2 --seed 42
```

空间块与整 session 划分：

```bash
.venv/bin/python rir_localization/split_dataset.py \
  --dataset rir_localization/artifacts/dataset_real/dataset.csv \
  --output /tmp/rir_spatial_split --mode spatial_block --spatial-block-size-m 0.5

.venv/bin/python rir_localization/split_dataset.py \
  --dataset rir_localization/artifacts/dataset_real/dataset.csv \
  --output /tmp/rir_session_split --mode session
```

`spatial_block` 保证同一空间 block 不跨 split，`session` 保证整个 session 不跨 split；数据组数
不足时可能产生空 split，并在 `split_stats.json` 明确警告。每次训练会把实际 split 固化到 run。

## 4. Smoke test（当前允许执行的训练）

默认 `config.yaml` 使用 CH0–CH6、500 ms、`normalization: none` 和 RIR-only regression。
保留原始幅度是保守选择，因为绝对幅度可能携带距离信息；可配置 `peak` 或 `rms`，但它们会
消除每条样本的全局幅度尺度。

```bash
.venv/bin/python rir_localization/train.py \
  --config rir_localization/config.yaml \
  --smoke-test
```

`--smoke-test` 强制 1 epoch、最多 2 个 train batch、`num_workers=0`，随后重载 checkpoint、
执行 test evaluation、单 RIR inference，并额外验证 classification forward/loss/grid decode，
最终只在全部成功后输出 `SMOKE TEST PASS`。它绝不会继续正式训练。

## 5. 正式训练（只供以后使用，当前不要执行）

收集足够、有空间覆盖、正确固定远端 Tx 坐标且 ToF 标定合理的数据并确认后：

```bash
.venv/bin/python rir_localization/train.py --config rir_localization/config.yaml
```

断点恢复：

```bash
.venv/bin/python rir_localization/train.py \
  --config rir_localization/config.yaml --resume rir_localization/runs/.../checkpoints/last.pt
```

坐标 mean/std 只从 train split 计算，并存入 checkpoint。最佳模型按 validation median
localization error（反归一化后的米制欧氏误差）选择。训练支持 CPU/CUDA auto、AdamW、AMP、
gradient clipping、ReduceLROnPlateau、early stopping、best/last checkpoint 和 deterministic seed。

## 6. 评估

```bash
.venv/bin/python rir_localization/evaluate.py \
  --checkpoint rir_localization/runs/.../checkpoints/best.pt \
  --split test
```

checkpoint 自带正确通道、长度、RIR normalization、坐标 transform、模型配置与 split 路径。
评估输出 mean/median/P50/P75/P90/P95/RMSE/min/max 米制误差、CDF、GT/prediction scatter、
histogram、spatial error map 和 `predictions.csv`。

## 7. 单条推理

```bash
.venv/bin/python rir_localization/infer.py \
  --checkpoint rir_localization/runs/.../checkpoints/best.pt \
  --rir /path/to/rir_float32.npy

.venv/bin/python rir_localization/infer.py \
  --checkpoint rir_localization/runs/.../checkpoints/best.pt \
  --measurement /path/to/measurements/000123
```

若未来启用 `model.use_tof_feature: true`，measurement 模式会从 `result.json` 读取 ToF；单 NPY
模式需额外提供 `--tof-seconds`。

## 8. Classification probability map

把配置改为：

```yaml
task: classification
model:
  type: classification_1dcnn
  base_channels: 32
  dropout: 0.2
  use_tof_feature: false
classification:
  grid_size_m: 0.25
```

网格边界只由 train split 拟合；网络输出 location-cell logits，loss 为 cross entropy，推理输出
最大概率 cell 中心和 top-k 概率。当前只做过 smoke 接口验证，没有正式训练。

## 9. KNN baseline

```bash
.venv/bin/python rir_localization/baselines.py \
  --config rir_localization/config.yaml \
  --train rir_localization/runs/.../splits/train.csv \
  --test rir_localization/runs/.../splits/test.csv \
  --output rir_localization/runs/.../knn_baseline --k 3
```

该 baseline 使用每通道 12 个时间窗的 log-RMS、log peak、归一化 peak index 和全局 log-RMS；
只用 train 特征 mean/std 标准化，再以欧氏距离做 inverse-distance weighted KNN。它是明确记录的
工程比较基线，不声称复现论文未公开的精确 KNN 特征。

## 文件职责

- `inspect_dataset.py`：真实 session/metadata/RIR/空间/ToF 数据审计。
- `build_dataset.py`：只读构建索引、排除表、统计和图。
- `split_dataset.py`：sequential / spatial block / session split。
- `dataset.py`：shape 自动识别、crop/pad、normalization、坐标和网格 transform。
- `model.py`：length-agnostic lightweight 1D CNN、ToF fusion、regression/classification head。
- `checkpoint.py`：安全、显式的 checkpoint 加载入口。
- `train.py`：训练、resume、checkpoint、history 和受限 smoke test。
- `evaluate.py`：米制指标、预测表和四类可视化。
- `infer.py`：NPY 或 measurement 目录单条推理。
- `baselines.py`：可复现低维特征 KNN。
- `utils.py`：字段读取、布局判断、seed、CSV/JSON 和公共指标。

## 正式训练前 Gate

当前结论是 **NOT READY FOR FORMAL TRAINING**。至少需要：

1. 固定远端 Tx，并把其坐标转换/记录到与 Rx 相同的 world frame；不要用本机 t1 speaker pose
   冒充远端 C2 Tx。
2. 在目标房间采集足够多 PASS 点，覆盖有意义的二维面积，减少 5 cm 内近重复并保留独立空间区。
3. 校准或修正当前 preliminary ToF；现有约 45–53 ms（15–18 m）与当前小型空间记录不一致。
4. 正式采集后重新运行 inspect/build/split，确认 CH7、shape、frame、时间基准和 quality gate；
   不要沿用这 6 条 smoke 样本的结论替代新数据审计。
