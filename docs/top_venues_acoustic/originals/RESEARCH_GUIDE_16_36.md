# “客厅听到喊叫并判断具体房间”论文导读（16–36）

## 结论先行

现有顶会论文中，没有一篇能够在任意住宅里开箱即用地完成“仅在客厅放一个麦克风，隔墙听到喊叫后直接输出具体房间”。最接近的技术路线是：

1. 用 **MAVL（NSDI 2021）** 处理人声、墙后/另一房间、NLoS 和多径反射；
2. 用 **Ubicoustics / ProtoSound / SPECTRA** 先确认声音是不是“喊叫”，并适配具体家庭和家庭成员；
3. 在可以每个房间放一个廉价麦克风节点时，采用 **HomeSound** 的分布式方案，房间判别会明显容易且更可靠；
4. 用 **SoundSpaces 2.0**、Audio-Visual Floorplan Reconstruction 和已有 RIR 论文做跨房间仿真、户型先验和数据增强。

这里的关键是把任务拆成两个输出：

- `event_type = shout / non-shout`：声音事件识别；
- `source_room = bedroom / kitchen / ...`：跨房间 NLoS 声源定位或房间分类。

RIR 生成只能为第二个任务提供仿真和房间声学特征，不能单独解决“喊叫识别 + 房间判别”。

## 选取口径

- 用户指定：AAAI、IJCAI、CVPR、ICCV、ICLR、ICML、NeurIPS、MobiCom、MobiSys、SenSys、CHI、UbiComp、UIST、IMWUT、Pervasive。
- 补充 A 类：仅加入 **NSDI 2021 的 MAVL**；NSDI 是公认的顶级系统会议，而且该论文直接研究 NLoS 人声定位。
- 编号 15 原先已被 EchoTag 占用，因此没有覆盖旧文件，新增内容从 16 开始。
- Computer Speech & Language、ICASSP、DCASE、ICRA、WACV 等虽有相关论文，但未进入本批正式语料库。

## 优先级说明

- **S：最直接**——应优先精读和复现。
- **A：关键模块**——构成实际系统不可缺少的部分。
- **B：方法借鉴**——场景或假设与目标有明显差异。
- **C：声学建模背景**——主要用于仿真、数据增强或声学表征，不直接输出房间。

## 新增论文清单

| 编号 | 级别 | 论文与 venue | 对需求的价值 | 重要边界 | 公开来源 |
|---:|:---:|---|---|---|---|
| 16 | S | MAVL: Multiresolution Analysis of Voice Localization，NSDI 2021 | 单个智能音箱麦克风阵列定位人声；明确讨论人在墙后或另一房间的 NLoS 场景；报告 NLoS 中位误差 0.47 m | 需要先主动发 chirp 建立房间轮廓；依赖矩形房间和主要一次反射等假设，不是端到端“房间标签”系统 | [USENIX](https://www.usenix.org/conference/nsdi21/presentation/wang) |
| 17 | B | Symphony: Localizing Multiple Acoustic Sources with a Single Microphone Array，SenSys 2020 | 单阵列、多声源、真实家居异常声音监测，可用于多喊叫者或电视等干扰源 | 论文要求 LoS；不解决隔墙房间判别 | [作者 PDF](https://wangwg1996.github.io/files/PDF/Symphony_sensys.pdf) |
| 18 | B | Voice Localization Using Nearby Wall Reflections (VoLoc)，MobiCom 2020 | 利用直达声与附近墙反射定位未知人声，是多径定位的重要基线 | 论文明确说明用户和阵列位于不同房间时会因 LoS 阻断而失败 | [作者 PDF](https://synrg.csl.illinois.edu/papers/voloc_mobicom20.pdf) |
| 19 | A | Indoor Localization without Infrastructure Using the Acoustic Background Spectrum，MobiSys 2011 | 用环境声谱作为房间指纹；相邻房间区分是核心实验，可启发房间分类器 | 定位的是“携带麦克风的设备所在房间”，不是远端喊叫者所在房间 | [USENIX](https://www.usenix.org/conference/mobisys-2011/indoor-localization-without-infrastructure-using-acoustic-background) |
| 20 | A | SoundSense: Scalable Sound Sensing for People-Centric Applications on Mobile Phones，MobiSys 2009 | 通用声音事件检测的经典系统，适合作为“喊叫/非喊叫”前端的历史基线 | 移动端通用事件分类，不含跨房间定位 | [作者 PDF](https://alumni.media.mit.edu/~panwei/pub/S3_mobisys09.pdf) |
| 21 | A | Ubicoustics: Plug-and-Play Acoustic Activity Recognition，UIST 2018 | 用声音效果库和增强训练通用家居声音分类器；可扩展“喊叫、哭声、跌倒声”等类别 | 只识别事件，不输出声源房间 | [项目页](https://www.figlab.com/research/2018/ubicoustics) |
| 22 | S | HomeSound: An Iterative Field Deployment of an In-Home Sound Awareness System，CHI 2020 | 每个房间部署节点、在户型图上显示房间级声音，并加入自动分类；与目标系统形态最接近 | 房间来自节点位置而非单客厅阵列推断；跨房间串音会造成多房间同时触发 | [项目页](https://makeabilitylab.cs.washington.edu/project/smarthomedhh/) |
| 23 | A | ProtoSound: A Personalized and Scalable Sound Recognition System，CHI 2022 | 少样本添加家庭自定义声音；适合为具体住户采集少量喊叫样本并适配环境 | 解决声音类别个性化，不解决房间定位 | [作者 PDF](https://stevenmgoodman.com/papers/Jain_CHI2022_Protosound.pdf) |
| 24 | A | SPECTRA: Personalizable Sound Recognition through Interactive Machine Learning，CHI 2025 | 新的端到端交互式采集、训练、测试流程；适合持续纠正家庭中的误报和漏报 | 偏人机交互与个性化流程，定位后端仍需另建 | [项目页](https://makeabilitylab.cs.washington.edu/project/spectra/) |
| 25 | B | Semantic Audio-Visual Navigation，CVPR 2021 | 面向短暂、语义化声音；声音停止后仍可利用记忆寻找来源 | 假设移动机器人主动导航，不是固定客厅麦克风 | [CVF](https://openaccess.thecvf.com/content/CVPR2021/html/Chen_Semantic_Audio-Visual_Navigation_CVPR_2021_paper.html) |
| 26 | B | Learning to Set Waypoints for Audio-Visual Navigation，ICLR 2021 | 论文示例就是寻找另一房间响铃的电话；声学记忆和 waypoint 可用于移动设备找人 | 依赖机器人移动、RGB-D/视觉和仿真环境 | [OpenReview](https://openreview.net/forum?id=cR91FAodFMe) |
| 27 | A | Sound Adversarial Audio-Visual Navigation，ICLR 2022 | 显式加入移动干扰声和类别/音量变化，适合研究电视、音乐、家电等家庭干扰 | 仍是主动导航任务；“攻击声”不等同于真实家庭噪声分布 | [OpenReview](https://openreview.net/forum?id=NkZq4OEYN-) |
| 28 | A | Audio-Visual Floorplan Reconstruction，ICCV 2021 | 从远处声音和视觉线索推断不可见房间与语义户型，为“声音从哪扇门/哪个房间传来”提供拓扑先验 | 主要输出户型而不是实时喊叫房间 | [CVF](https://openaccess.thecvf.com/content/ICCV2021/html/Purushwalkam_Audio-Visual_Floorplan_Reconstruction_ICCV_2021_paper.html) |
| 29 | A | SoundSpaces 2.0: A Simulation Platform for Visual-Acoustic Learning，NeurIPS 2022 | 可在扫描户型中渲染任意声源、麦克风位置和 RIR；适合生成跨房间、开关门、噪声和不同喊叫位置的数据 | 仿真到真实住宅存在域差异，必须配合真实录音验证 | [NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2022/hash/3a48b0eaba26ba862220a307a9edb0bb-Abstract-Datasets_and_Benchmarks.html) |
| 30 | B | Sound Localization from Motion，ICCV 2023 | 自监督学习双耳声音方向和相机旋转；适合移动机器人或可旋转阵列积累方向线索 | 单帧固定阵列和隔墙房间标签不是其目标 | [CVF](https://openaccess.thecvf.com/content/ICCV2023/html/Chen_Sound_Localization_from_Motion_Jointly_Learning_Sound_Direction_and_Camera_ICCV_2023_paper.html) |
| 31 | C | Self-Supervised Visual Acoustic Matching，NeurIPS 2023 | 学习并分离房间声学特征，可用于域适配和声学指纹表示 | 目标是声学匹配/重合成，不做声源定位 | [NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2023/hash/4cbec10b0cf25025e3f9fcfd943bb58c-Abstract-Conference.html) |
| 32 | C | Visual Acoustic Matching，CVPR 2022 | 从视觉和音频学习目标空间的声学特征，是 31 的直接前作 | 不输出声源房间 | [CVF](https://openaccess.thecvf.com/content/CVPR2022/html/Chen_Visual_Acoustic_Matching_CVPR_2022_paper.html) |
| 33 | C | Image2Reverb: Cross-Modal Reverb Impulse Response Synthesis，ICCV 2021 | 从单张图像生成 RIR，可作为缺少实测 RIR 时的数据增强方法 | 生成“看起来合理”的 RIR，不等于对真实住宅进行测量或定位 | [CVF](https://openaccess.thecvf.com/content/ICCV2021/html/Singh_Image2Reverb_Cross-Modal_Reverb_Impulse_Response_Synthesis_ICCV_2021_paper.html) |
| 34 | C | Novel-View Acoustic Synthesis，CVPR 2023 | 给定新听者位置合成空间音频，可补足训练位置采样 | 仍是声场合成，不是房间分类器 | [CVF](https://openaccess.thecvf.com/content/CVPR2023/html/Chen_Novel-View_Acoustic_Synthesis_CVPR_2023_paper.html) |
| 35 | C | Be Everywhere – Hear Everything (BEE)，ICCV 2023 | 用少量 A/V 接收器重建动态场景中任意位置音频，适合研究稀疏传感器布局 | 目标为高保真音频重建，且需要多个 A/V 接收器 | [CVF](https://openaccess.thecvf.com/content/ICCV2023/html/Chen_Be_Everywhere_-_Hear_Everything_BEE_Audio_Scene_Reconstruction_by_ICCV_2023_paper.html) |
| 36 | B | WALRUS: Wireless Acoustic Location with Room-Level Resolution Using Ultrasound，MobiSys 2005 | 经典房间级定位，利用墙对超声的天然边界；可作为“每房间主动信标”方案 | 必须由房间设备主动发超声并与无线消息同步，不能定位被动喊叫者 | [USENIX PDF](https://www.usenix.org/legacy/event/mobisys05/tech/full_papers/borriello/borriello.pdf) |

## 已由 PDF 正文核实的引用链

下列关系不是根据主题相似度猜测，而是已在下载 PDF 的正文或参考文献中核实：

1. **MAVL（16，NSDI 2021） → VoLoc（18，MobiCom 2020）**
   MAVL 将 VoLoc 作为最相关的单阵列多径人声定位工作，并在 LoS、NLoS、阵列尺寸和远距离条件下比较。

2. **Symphony（17，SenSys 2020） → VoLoc（18，MobiCom 2020）**
   Symphony 从 VoLoc 的单声源设定扩展到多声源，并给出直接性能对比。

3. **已有 Deep Room Recognition（01，IMWUT 2018） → ABS（19，MobiSys 2011）和 WALRUS（36，MobiSys 2005）**
   01 的正文和参考文献分别把两者作为被动环境声指纹与主动超声房间定位的经典前作。

4. **ProtoSound（23，CHI 2022） → Ubicoustics（21，UIST 2018）、HomeSound（22，CHI 2020）、SoundSense（20，MobiSys 2009）**
   这条链展示了从通用移动声音感知、通用声学活动识别和家庭部署，发展到少样本个性化识别的路径。

5. **SPECTRA（24，CHI 2025） → ProtoSound（23，CHI 2022）和 HomeSound（22，CHI 2020）**
   SPECTRA 明确讨论把自己的交互前端与 ProtoSound 后端结合，并回顾 HomeSound。

6. **Sound Adversarial AV Navigation（27，ICLR 2022） → Semantic AV Navigation（25，CVPR 2021）和 Waypoints（26，ICLR 2021）**
   27 将 25、26 作为主要音视导航前作，并补充复杂干扰声音。

7. **SoundSpaces 2.0（29，NeurIPS 2022） → Semantic AV Navigation（25）、Waypoints（26）、Audio-Visual Floorplan Reconstruction（28）**
   29 将这些论文列为平台支撑的导航与户型重建任务。

8. **Self-Supervised VAM（31，NeurIPS 2023） → VAM（32，CVPR 2022）、Image2Reverb（33，ICCV 2021）、NVAS（34，CVPR 2023）**
   这是房间声学表征与生成方向最清楚的一条新旧顶会引用链。

## 推荐阅读与实施顺序

如果只能看 6 篇，建议按以下顺序：

1. **16 MAVL**：判断单客厅阵列做跨墙 NLoS 的可行边界；
2. **22 HomeSound**：比较“单阵列推断”和“每房间一个节点”两种系统架构；
3. **21 Ubicoustics**：建立喊叫事件检测前端；
4. **23 ProtoSound**：让检测器适配具体住户、具体住宅和少量样本；
5. **29 SoundSpaces 2.0**：生成覆盖声源房间、门状态、距离和噪声的训练数据；
6. **19 ABS**：研究稳定的房间声学指纹，避免只用瞬时响度判断。

## 建议继续检索的关键词

- `multi-room voice localization`, `cross-room acoustic source localization`
- `NLoS voice localization`, `through-wall acoustic localization`
- `room-level sound event localization`, `room-localized speech activity detection`
- `distributed microphone smart home`, `ad-hoc microphone array room classification`
- `doorway diffraction sound localization`, `multipath acoustic localization floorplan`
- `open-set domestic sound event detection`, `few-shot personalized sound recognition`
- `sim-to-real room impulse response`, `multi-room RIR dataset door open closed`

## 实验设计上应重点覆盖

- 门全开、半开、关闭；相邻房间与隔一个房间；同楼层与跨楼层；
- 不同喊叫者、音量、方向、词语和持续时间；
- 电视人声、手机外放、儿童哭声、犬吠、吸尘器等混淆源；
- 客厅阵列不同摆放位置与遮挡；家具改变和混响变化；
- 输出除准确率外，还要报告未知声音拒识、漏报、误报、跨家庭泛化以及从事件发生到报警的延迟。
