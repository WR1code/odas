# Paper阅读笔记问题模版（精读）

**Title:** AV-RIR: Audio-Visual Room Impulse Response Estimation

**Authors:** Anton Ratnarajah, Sreyan Ghosh, Sonal Kumar, Purva Chiniya, Dinesh Manocha

**Published in:** IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR), 2024

**Pages:** 27164–27175（所附 PDF 共 12 页；正文 1–8 页，References 9–12 页）

---

#### 1. 你从这篇论文中能够总结的信息

【作者原文】

这篇论文研究的是**从一段已经带有房间混响的语音，再结合对应房间的视觉信息，直接估计该环境的 Room Impulse Response（房间脉冲响应，RIR）**。作者提出 AV-RIR，一个多模态、多任务学习框架。它的输入不是标准的测量扫频信号，而是：

- 一段 source reverberant speech（源环境中的混响语音）；
- 源环境的 RGB panoramic image（RGB 全景图）；
- 作者构造的 Geo-Mat feature（几何-材料特征）。

AV-RIR 同时学习两个任务：主任务是 RIR estimation，辅助任务是 speech dereverberation。作者认为，从式 (1) `S_R = S_C ⊛ RIR` 出发，如果模型同时学习“从混响语音恢复干净语音”和“从混响语音恢复 RIR”，本质上是在学习把混响语音分解成 clean speech 与 RIR 两部分。

方法中有三个特别重要的设计：

1. **神经音频 codec / RVQ 架构**：用编码器、Residual Vector Quantizer（RVQ）和解码器学习 RIR 与语音的离散潜在表示。
2. **Geo-Mat feature**：从全景图中识别物体和材料，把材料吸声系数 AC 与单目深度图编码成三通道特征，使视觉输入不仅有 RGB 外观，还有与声传播直接相关的“材料 + 几何”信息。
3. **CRIP（Contrastive RIR-Image Pre-training）**：建立图像与 RIR 的对比学习共同嵌入空间；推理时根据场景图像从一个 RIR 数据库中检索相似 RIR，用其后半段替换网络估计 RIR 的 late reverberation，从而弥补神经网络对噪声状晚期混响难以精确生成的问题。

实验主要使用 SoundSpaces + LibriSpeech 的合成数据，并用 AVSpeech 的真实网络视频音频测试语音去混响泛化。RIR 估计指标包括 T60 Error、DRR Error、EDT Error、early-component MSE（EMSE）和 late-component MSE（LMSE）；语音去混响使用 WER、EER 和 RTE。作者报告 AV-RIR 相比 S2IR-GAN 在 T60、DRR、EDT、EMSE、LMSE 上分别提升 36%、42%、63%、89%、98%；感知测试中 56%–79% 的参与者认为 AV-RIR 生成的声音最接近 GT。

【我的分析】

这篇文章的核心不是“在空间任意两点预测 RIR 场”，而是**给定某个环境中已经录到的混响语音 + 环境视觉线索，估计该录音对应的 RIR**。因此它与 Neural Acoustic Field、FewShotRIR、RAF 等“位置条件化、稀疏 RIR 插值 / 连续声场”工作的任务定义不同。

对机器人研究最值得借鉴的部分不是整套 AV-RIR 原封不动搬过去，而是两点：

- **用视觉估计材料/几何并作为声学先验**，可降低只靠声音辨识环境的歧义；
- **将早期 RIR 与晚期混响分开建模**，早期部分由与声源/接收位置更相关的网络估计，晚期部分由场景级几何/材料先验补足。

---

#### 2. 这篇论文的Motivation是什么（应用需求角度/技术角度）

【作者原文】

**应用需求角度：** RIR 是声源与接收端之间的声学传递函数，可以描述直达声、早期反射和晚期混响。AR/VR、空间音频、声音渲染等应用要求虚拟声效与视觉场景的声学属性一致，否则会产生作者所称的 “room divergence effect”，降低沉浸感。真实环境 RIR 的直接测量需要专业硬件与人工；物理仿真又需要准确 3D mesh 和材料信息，因此作者希望直接从容易获得的真实混响语音和图像中估计 RIR。

**技术角度：** 以前直接从混响语音估计 RIR 的方法主要是 audio-only。作者指出，RIR 的 early components 呈稀疏脉冲结构，而 late components 幅度更小、呈噪声状，纯音频神经网络通常只能较好恢复前者，后者往往只能用衰减滤波噪声近似。另一方面，仅由 RGB 图像生成 RIR 又缺少足够的几何、材料、声源信息。因此作者希望把 audio 与 visual cues 结合起来，并显式加入材料吸声与几何深度先验。

【我的分析】

Motivation 可以概括为一句话：**仅声音对环境信息不充分，仅图像对声学信息也不充分，因此把二者融合，并把“可学习的 RIR 分解”作为目标。**

对于真实机器人，这个思路尤其有意义：机器人通常本来就有 RGB/RGB-D 相机，如果声学模型只用麦克风数据，相当于丢掉已经存在的大量几何和材料线索。

---

#### 3. 为什么这个问题没有得到解决？以前的方法有哪些问题？解决这些问题的难点？（Challenge）

【作者原文】

作者归纳的以前方法问题主要有：

1. **传统信号处理式 RIR 估计依赖较强假设。** 有的方法假设输入源是调制高斯脉冲而不是自然语音；有的方法需要提前知道扬声器或麦克风的具体属性。
2. **Audio-only neural RIR estimation 对 late reverberation 不够好。** early RIR 稀疏、脉冲性强，late RIR 噪声状、幅值低，二者统计结构差异明显。
3. **Visual-only RIR generation 信息不足。** 单张 RGB 图看不到精确三维结构、材料吸声属性、扬声器位置等完整声学条件。
4. **物理仿真代价高。** 准确声学模拟往往要求 3D mesh 和完整材料参数，而且很难在交互速率下复现全部声学效应。
5. **真实场景中往往没有同一房间预先测好的 RIR，也没有完整 3D 几何。** 这使得依赖“少量同场景实测 RIR + 精确几何”的方法使用受限。

真正的技术 Challenge 有三类：

- 如何从自然混响语音中分离 clean speech 与 RIR；
- 如何把图像中“看起来是什么”转换成对声传播有意义的“吸声系数 + 深度”；
- 如何同时恢复稀疏早期反射与噪声状晚期混响，而不是只优化其中一种结构。

【我的分析】

对于机器人应用还会多出论文没有正面解决的 Challenge：

- 移动机器人上的麦克风会有电机、风扇、底盘噪声；
- 人声通常非平稳、声源会移动；
- 跨房间时存在门洞绕射、墙体透射、多次反射，单个房间全景图未必能描述传播路径；
- 如果目标是“判断声音来自哪个房间”，还需要把估计出的 RIR / acoustic embedding 与地图中的房间身份绑定，而 AV-RIR 本身没有做 room classification 或 source localization。

---

#### 4. 系统架构？他们提出的方法是什么？（假设？手段？设计？）

【作者原文】

整体架构见论文 Fig. 2，可按以下流程理解：

1. 输入混响语音 `S_R`，Reverberant Speech Encoder `E_R` 先产生共享潜在表示。
2. 网络分成两条支路：
   - **RIR estimation branch**：进一步通过 RIR Encoder，与 Geo-Mat feature 的 ResNet-18 编码融合；经过 RVQ 后由 RIR decoder 输出 RIR 的 early components。
   - **Speech dereverberation branch**：通过 Speech Encoder，与 RGB panoramic image 的 ResNet-18 编码融合；经过 RVQ 后由 HiFi-GAN vocoder 输出 enhanced / clean speech。
3. Geo-Mat feature 由材料吸声信息和深度组成。Tag2Text 识别图中物体，Grounding DINO 给出目标区域，再用材料吸声数据库和 Sentence Transformer 做语义匹配，获得不同材料在 125、500、2000、8000 Hz 的 AC。
4. CRIP 单独做 image–RIR contrastive pretraining。推理时用场景图像检索最相似 RIR，并把检索 RIR 的 late component 加入/替换到网络预测中。
5. 最终 estimated RIR 可与任意 target clean speech 卷积，使其听起来像在源环境中说话。

作者的主要假设包括：输入是**stationary single-talker speech 或无噪声 single-source audio**；论文的主要训练数据来自 SoundSpaces 合成 RIR。

【我的分析】

这个系统实际上是一个“**音频负责观测当前声学传递，视觉负责补充场景先验，检索负责补充难生成的后混响**”的混合模型。它不是单纯端到端黑盒，而是把不同信息源分配给最擅长的部分，这也是其工程上最有价值的地方。

---

#### 5-1. 他们的observation是什么（他们发现了什么现象？）

【作者原文】

论文中最关键的 Observation 有：

1. RIR 的 **early components 与 late components 结构不同**：前者具有明显的稀疏、脉冲结构，后者幅度更低且更像噪声。
2. 现有 neural RIR estimators 对 early components 的估计明显容易于 late reverberation。
3. late reverberation 与房间几何 / layout 相关，因此场景图像能够为其提供额外信息。
4. 深度图能够提升 audio-visual dereverberation；物理 RIR simulator 本身也依赖 geometry + material absorption coefficient，所以把这两类物理信息编码进视觉输入是合理的。
5. 在 RVQ 中放宽码率到约 59 Kbps 会改善性能，但继续增加码率收益不明显。
6. Geo-Mat 三个通道的具体排列顺序对结果影响不大。

【我的分析】

最关键的一条 Observation 是：**“RIR 不是统计性质完全一致的一整段波形。”** 如果把早期反射和晚期混响当成同一种回归目标，用同一种损失从头到尾拟合，很可能训练目标本身就不合理。AV-RIR 的结构正是围绕这个观察展开。

---

#### 5-2. 他们的insight是什么（他们发现这样的现象后，能做什么？）

【作者原文】

由上述观察，作者得到几个直接设计 Insight：

- 既然 `S_R = S_C ⊛ RIR`，就把 RIR estimation 与 speech dereverberation 放进同一个 multi-task framework，让两个任务互相约束。
- 既然图像本身没有显式声学材料参数，就从物体识别结果映射到材料吸声系数，形成 Geo-Mat feature。
- 既然神经网络难以精确生成噪声状 late reverberation，就不强迫网络独立生成全部 late RIR，而是通过 CRIP 从大规模 RIR 数据库检索最符合场景的后混响。
- 既然音频和视觉是互补信息，就在潜在空间中进行融合，而不是只选择其中一种模态。

【我的分析】

对于机器人主动声学建模，可以进一步抽象成：**把“位置相关的声学信息”和“环境级声学先验”分开。** 机器人移动采集到的 RIR / 声音可以提供位置相关线索；视觉材料、房间布局可提供环境级先验。这个思想比“直接拿 AV-RIR 网络做房间分类”更值得迁移。

---

#### 6. 具体是怎么实现的？

【作者原文】

**(1) Reverberant Speech Encoder**

作者使用简单的 1D CNN，单输入、单输出通道。其输出作为 RIR estimation 与 speech dereverberation 两个任务的共享表示。

**(2) RIR Encoder**

由 S2IR-GAN encoder 改造而来。三层输出通道为 256 / 512 / 1024，kernel length 分别为 14401 / 41 / 41，stride 为 225 / 2 / 2。输入每段混响语音为 14400 samples，最终形成 `1024 × 16` 的时序声学特征；第一层超大卷积核用于快速编码 RIR 结构。

**(3) Vision Encoders**

RGB panoramic image 与 Geo-Mat feature 分别由独立 ResNet-18 编码，输出 reshape 为 `1024 × 4`。

**(4) Multi-modal Fusion**

沿时间轴把视觉特征与音频流融合，再投影到设计好的潜在空间，送入 RVQ。

**(5) RVQ**

基于 SoundStream 的 Residual Vector Quantizer。作者放松原本面向低码率音频流的 bitrate 约束，将压缩设为约 59 Kbps；使用 `Nq = 64` 层 VQ、codebook size `N = 8192`。

**(6) Decoders**

speech branch 使用 HiFi-GAN vocoder；RIR branch 使用修改后的 SoundStream decoder，包含 6 个 transposed-convolution blocks，输出通道 `(256, 128, 64, 32, 32, 16)`，stride `(5, 5, 2, 2, 1, 1)`，最后用 kernel=1、stride=1 的 1D Conv 投影回 waveform domain。

**(7) Geo-Mat**

Tag2Text → Grounding DINO → 材料数据库 → Sentence Transformer 语义匹配，得到每个物体最相似材料的吸声系数。使用 125、500、2000、8000 Hz 四个子带 AC。三通道构造为：

- `I_G[:,:,0] = AC_125 + AC_500 × 16`
- `I_G[:,:,1] = AC_2000 + AC_8000 × 16`
- `I_G[:,:,2] = I_D`（深度图）

若数据集没有 depth，作者采用 Godard et al. 的单目深度系统估计。

**(8) Training losses**

RIR 用 time-domain MSE；speech dereverberation 使用多尺度 Mel-spectrogram loss、STFT magnitude + phase loss，以及 adversarial loss。两个 RVQ codebook 分别有 VQ loss。最终 generator loss 将 metric、adversarial、VQ losses 加权组合。

Mel loss 使用窗口长度 `{64,128,256,512,1024,2048,4096}`。STFT loss 同时约束幅度谱和相位；相位通过 `sin/cos` 映射到单位圆直角坐标，以避免 phase wraparound。

**(9) CRIP**

CRIP 类似 CLIP：HorizonNet encoder 编码 panoramic image；FAST-RIR 的 discriminator 网络作为 RIR encoder。图像和 RIR 都映射到 1024 维 joint embedding，使用双向对比损失训练。

推理时，根据图像 embedding 与数据库 RIR embedding 的 cosine similarity 检索 RIR。作者超参数搜索后选择 `S = 2000`，即用 retrieved RIR 的 samples `[2000:4000]` 替换估计 RIR 的对应 late part。

**(10) Training setting**

AV-RIR：batch size 16；先用 metric + VQ loss 训练 400 epochs，再用总损失训练 1K epochs；Adam，`β1=0.5, β2=0.9`，learning rate `5×10^-5`，每 200K steps 将学习率乘 0.5。

---

#### 7. 作者怎么评估系统的？

【作者原文】

**Dataset / data source**

- SoundSpaces：主要训练与评估数据。RIR 来自几何声学模拟，环境来自 Matterport3D；clean speech 来自 LibriSpeech，再与 RIR 卷积生成 reverberant speech。
- AVSpeech：真实网络视频音频，只用于测试 speech dereverberation，因为没有对应 GT RIR。
- CRIP datastore：由 SoundSpaces 中的 synthetic RIR 构成，并排除 test-set RIR。

**RIR estimation metrics**

- T60 Error：估计 RIR 与 GT RIR 的混响时间差；
- DRR Error：direct-to-reverberant ratio 误差；
- EDT Error：early decay time 误差；
- EMSE：early component 的时域 MSE；
- LMSE：late component 的时域 MSE。

**RIR baselines**

Image2Reverb、VAM（仅用于 perceptual evaluation）、FAST-RIR++、CRIP-only、FiNS、S2IR-GAN。

**Speech dereverberation metrics**

- ASR：WER；
- Speaker Verification：EER；
- AVSpeech real-world test：RTE。

**Speech baselines**

WPE、MetricGAN+、DEMUCS、HiFi-GAN、VoiceFixer、SkipConvGAN、Kotha et al.；audio-visual baselines 为 VIDA、AdVerb。

**主要结果**

Table 1 中 AV-RIR 的 `T60 Error = 40.2 ms, DRR Error = 1.76 dB, EDT Error = 62.1 ms, EMSE = 82×10^-5, LMSE = 6×10^-5`。相比 S2IR-GAN，作者报告在五个指标上分别提升 36%、42%、63%、89%、98%。

Table 2 中 AV-RIR 得到 `WER = 4.17%`、`EER = 2.02%`、`RTE = 0.042 s`。在 SV 上比 AdVerb 更好；ASR 上除 AdVerb 外优于其他 baseline。AVSpeech 的真实录音 RTE 测试中，作者称相对 baselines 提升 60%。

**Ablation**

- Multi-task：RIR estimation 提升 31%–48%，speech dereverberation 提升 13%–21%。
- CRIP：LMSE 改善 86%。
- Geo-Mat：RIR estimation accuracy 改善 11%–28%。
- Visual cues：完整 AV-RIR 相比 audio-only variation，RIR estimation 提升 41%–55%，speech dereverberation 约提升 24%。

**Perceptual evaluation**

6 个场景，T60 约 0.2–0.7 s。参与者比较 Image2Reverb、VAM、AV-RIR 与 GT 的听感，AV-RIR 在 6 个场景获得 56%–79% 的最高偏好率。

【我的分析】

实验覆盖比较全面：既有 RIR 物理指标，也有 ASR/SV 下游指标，还有听感主观测试与 ablation。不过核心 RIR 训练/评估仍主要建立在 SoundSpaces 的 simulated RIR 上，因此它证明的是“模型在合成声学数据上有效，并对真实混响语音的去混响指标具有一定泛化”，并没有直接证明“真实房间中估出的 RIR 波形已经达到实测级精度”。

---

#### 8. 你对这篇文章的问题、手段、评估有什么样的分析？

【我的分析】

**问题定义：** 很有价值，因为它把 RIR acquisition 从“必须发测试信号”转向“利用自然混响语音 + 视觉”。对 AR/VR 和移动终端尤其现实。

**手段：** AV-RIR 的方法不是单一创新点，而是多个合理模块的组合：codec/RVQ、multi-task、Geo-Mat、CRIP。最大的设计价值在于它把不同困难拆开处理：语音分支负责恢复 clean speech，RIR 分支负责 early RIR，CRIP 负责 late RIR，视觉材料模块提供物理先验。

**评估：** 指标选择合理，尤其 EMSE / LMSE 将 early 与 late component 分开评估，能直接对应论文提出的问题。不过论文对真实世界 RIR estimation 的验证仍不足：AVSpeech 没有 GT RIR，所以真实数据只能用去混响 RTE 间接验证。

**对于机器人：** 如果机器人已经有 RGB-D 相机，Geo-Mat 的“视觉→材料/几何声学先验”值得直接借鉴。但如果目标是跨房间声源识别，这篇论文没有显式解决声源位置、跨门传播和房间分类，必须增加地图 / room ID / source localization 模块。

---

#### 9. 这是篇好文章吗？有什么瑕疵？未来有哪些可以改进的地方？

【我的分析】

我认为它是一篇方法思路完整、实验较充分的好文章，尤其适合作为“音视频融合 RIR 估计”主线参考。但有几个明显瑕疵：

1. **真实 RIR ground truth 验证不足。** 主要 RIR 精度来自 SoundSpaces 模拟数据；真实 AVSpeech 没有 GT RIR。
2. **输入假设偏理想。** 作者明确假设 stationary single-talker / single-source、无噪声；这与真实机器人移动、风扇噪声、电机噪声、人声重叠有较大差距。
3. **CRIP 是检索式补偿。** 它不是从目标房间真正重建 late reverberation，而是从数据库找相似 RIR 并替换后段；性能依赖 datastore 的覆盖度。
4. **Geo-Mat 材料识别存在链式误差。** 物体识别错误、材料语义匹配错误、材料数据库 AC 与真实物体表面处理不同都会累积。
5. **没有测试 moving source / moving receiver。** 论文结论最后也把 moving sources 作为 future work。
6. **没有跨房间传播实验。** 方法面向“对应环境”的图像和混响语音，并未验证人在卧室、机器人在客厅这种 coupled-room / NLOS 情形。

未来可以改为：使用真实 RAF/实测 RIR 做训练或 finetune；直接引入 RGB-D / 3DGS / mesh；引入多麦克风方向信息；把 CRIP 从数据库检索升级成条件生成 late field；加入移动源、噪声、多声源和跨房间数据。

---

#### 10. 最有意思和最具争议的问题是？

【我的分析】

**最有意思：** 论文没有把“估计整个 RIR”当成一个统一波形回归，而是承认 early / late component 本质不同，并用不同机制处理。尤其 CRIP 用视觉去检索 late RIR，是一种非常工程化但有效的办法。

**最具争议：** “检索到一个相似 RIR 的后半段并替换预测 RIR 的 `[2000:4000]` samples，是否算真正准确地估计了目标房间的 late reverberation？” 从任务指标上它有效，但物理上 retrieved RIR 并不是目标声源-接收端真实传播路径的直接测量结果。它更像**利用场景相似性得到合理的 late-reverb prior**。

另一个问题是 `S = 2000` 是通过超参数调优得到的固定切分点。不同采样率、不同房间 T60、不同声源-接收距离下，early/late transition 不一定应该固定在同一 sample index。

---

#### 11. 这个洞对实际情况来说能用吗？谁会用？他们用需要什么代价？什么时候会成为现实？

【作者原文】

作者面向 AR/VR、空间音频、speech processing 等场景。论文强调 reverberant speech 可以通过手机、Amazon Echo 等常见设备轻易获得，因此相比专业 RIR 测量，输入采集门槛更低。

【我的分析】

**能不能上真实机器人？** 可以迁移其中思想，但不能直接认为论文已经证明“真实移动机器人可用”。真实机器人至少需要处理噪声、相机视角变化、移动声源和真实材料偏差。

**是否要求固定麦克风？** 论文没有规定麦克风必须永久固定；但单个样本的 RIR 对应特定声源-接收关系，且论文假设输入源静止。若机器人移动，应该在短时间窗内近似静止，或把位姿显式作为条件。

**是否要求固定扬声器？** 论文输入是自然混响语音，不要求用扬声器播放 ESS/扫频信号；但它假设单一静止声源。若用机器人主动发声采集，可以把机器人扬声器作为受控 source，会比随机人声更容易标定。

**发射端和接收端能否互换？** 论文没有研究 reciprocity，也没有实验验证“交换 emitter/listener 后同一模型直接成立”。在线性、静止、互易声学介质中 RIR 理论上具有互易性，但扬声器/麦克风的方向性与硬件频响会使真实系统不完全对称；这属于基于声学理论的推断，不是本文实验结论。

**是否需要大量 RIR？** AV-RIR 主网络训练依赖大量 SoundSpaces synthetic RIR；CRIP 还依赖一个较大的 RIR datastore。它的优点是部署时输入不需要在目标环境预先逐点实测大量 RIR，但训练阶段仍然是数据密集型方法。

**新房间是否重新训练？** 论文目标是利用输入语音 + 图像推断新环境的 RIR，并不是为每个新场景单独训练一个 neural field，因此理论上不要求每个新房间重新训练。但真实世界跨域泛化程度没有用带 GT RIR 的大规模真实新房间数据充分证明。

**数据采集成本：** 相比传统 ESS 多点测量低，因为只需环境图像和自然混响语音；但若要训练自己的真实机器人版本，仍建议采集一批实测 ESS/RIR 作为 GT 做 finetune 与验证，否则无法知道真实环境误差。

**对低频、人声、跨房间的潜在问题：** 人声频带有限，对 RIR 全频带的激励不如 ESS；跨房间传播会加入门洞绕射、墙体透射和多房间耦合，单房间 panoramic image / Geo-Mat 可能不足。论文没有直接验证这些问题。

**什么时候能成为现实？** 用于 AR/VR 的“合理环境混响匹配”已经很接近实用；用于机器人做高精度、跨房间、位置可解释的 RIR 建模，还需要真实数据、位姿条件和多房间传播模型。

---

#### 12. 这篇文章的贡献？（ideas，methods，software，experimental result，实验技巧）

【作者原文】

1. 提出 AV-RIR：一个 audio-visual、multi-modal、multi-task 的 RIR estimation framework。
2. 设计 neural codec-based multi-modal architecture，并引入 Geo-Mat feature，将视觉中的 room geometry 与 material absorption properties 显式编码。
3. 用 speech dereverberation 作为辅助任务，使网络共同学习 clean speech 与 RIR 的分解。
4. 提出 CRIP，用 image-to-RIR retrieval 改善 late reverberation。
5. 在 SoundSpaces 上进行 RIR 估计、SLP downstream task、perceptual evaluation 和 ablation，报告显著提升。

【我的分析】

按贡献类型拆分：

- **Idea：** 音频 + 视觉 + 材料/几何先验联合估计 RIR；early / late 分治。
- **Method：** 双分支多任务 codec/RVQ、Geo-Mat、CRIP。
- **Experimental technique：** 把 EMSE 与 LMSE 分开；同时用物理声学指标、ASR/SV 下游指标和主观听感验证。
- **对机器人最可借鉴的实验技巧：** 真实部署时也应把“完整 RIR 波形 MSE”拆成可解释的 RT60/DRR/EDT + early/late error，而不是只看一个分类准确率。

---

#### 13. 这个研究的未来趋势与方向（作者观点，你的观点）

【作者原文】

作者明确写出的 future work：

- multi-channel RIR estimation；
- noisy environment；
- multi-source environment；
- moving sources。

【我的分析】

结合机器人声学，我认为下一步更重要的方向包括：

1. **Sim2Real / Real RIR finetuning**：利用 RAF 等真实密集 RIR 数据或自己机器人采集的 ESS 数据校准。
2. **位姿条件化**：显式输入 source pose、receiver pose、机器人 SLAM pose，而不是只用图像和混响语音。
3. **RGB-D / 3DGS / Mesh + material fusion**：把 Geo-Mat 从二维全景图升级成三维、可查询的 acoustic scene representation。
4. **跨房间 / coupled-room acoustic field**：建模门洞、走廊、墙体等对 NLOS 声音的传播。
5. **Room-level acoustic fingerprint**：不一定恢复完整 RIR，而是学习对房间身份稳定、对具体声源位置相对鲁棒的 embedding。
6. **Active acoustic exploration**：机器人在 SLAM 巡航时选择最有信息量的位置主动发 ESS/宽带声，减少新家庭的采集时间。
7. **Reciprocity-aware learning**：如果硬件标定允许，可利用声学互易性减少 source/receiver 配对采样量。

---

#### 14. Any questions?（你有什么疑问）

1. CRIP 固定替换 `[2000:4000]` samples 的物理依据有多强？换采样率、T60 或房间大小后是否仍最佳？
2. 如果 datastore 中不存在与目标房间几何/材料相似的 RIR，CRIP 会怎样退化？
3. Geo-Mat 用物体类别语义匹配材料数据库，但“sofa / curtain / painted wall”等同名材料的实际吸声系数差异可能很大，模型对 AC 错误有多敏感？
4. AV-RIR 在真实、带 ground-truth RIR 的房间上，RIR 波形与 T60/DRR/EDT 的绝对误差是多少？
5. 输入人声频谱没有覆盖的频段，估计出的 RIR 是否主要依赖训练先验而不是真实观测？
6. 如果麦克风或声源移动，短时输入是否仍可近似为一个 LTI RIR？模型需要怎样加入 trajectory / pose conditioning？
7. 对跨房间传播，单个 source-environment panoramic image 是否足够？是否需要把相邻房间、门洞和走廊都纳入视觉几何模型？
8. 对机器人应用，与其恢复完整 RIR，是否用 AV-RIR 中间层的 acoustic embedding 做 room-ID / room-origin classification 会更稳定、更轻量？
9. 如果机器人主动发宽带 ESS，而不是依赖自然语音，Geo-Mat + multi-task 的收益是否还会同样明显？
10. 能否把 AV-RIR 的 Geo-Mat 与 FewShotRIR / Neural Acoustic Field 结合：用少量真实 RIR + 3D 视觉材料先验，在新家庭快速建立连续声场？
