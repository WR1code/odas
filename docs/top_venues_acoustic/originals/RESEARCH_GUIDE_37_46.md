# “固定客厅接收端判断源房间”补充论文导读（37–46）

## 检索结论

在用户指定的 AAAI、IJCAI、CVPR、ICCV、ICLR、ICML、NeurIPS、MobiCom、MobiSys、SenSys、CHI、UbiComp/IMWUT、UIST、Pervasive，以及其他 A 类主会中，尚未发现一篇完整实现以下设定的论文：

> 只在客厅放置一个固定麦克风或小型阵列，从不同封闭房间自然传到客厅的喊叫中提取跨房间传播指纹，并直接输出源房间标签。

现有顶会工作分别覆盖家庭声源定位、房间/环境指纹、NLoS 主动声学成像、多说话人定位分离、去混响和跨房间仿真。37–46 是在不降低 venue 标准的前提下，与目标任务最接近或能直接支撑系统实现的一批补充论文。

## 论文清单

| 编号 | 级别 | 论文与 venue | 与目标任务的价值 | 关键边界 | 公开来源 |
|---:|:---:|---|---|---|---|
| 37 | S | Using Sound Source Localization in a Home Environment，Pervasive 2005 | 在真实住宅的客厅、餐厅、厨房区域被动定位说话、脚步、餐具等自然声音；输出三维声源位置，可进一步映射成房间/区域标签 | 使用 16 个分布式麦克风（4 个四麦阵列），覆盖相连的公共区域；不是客厅单阵列，也没有验证关闭房门后的跨房间定位 | [Georgia Tech 作者版本](https://repository.gatech.edu/items/12ca2de5-37ce-4f5e-940a-c854654968e3)、[Pervasive 2005 program](https://www.pervasive.ifi.lmu.de/doc/pervasive2005-program.pdf) |
| 38 | B | SoundCam: A Dataset for Finding Humans Using Room Acoustics，NeurIPS 2023 Datasets and Benchmarks | 提供 5,000 条十通道真实 RIR 和 2,000 条十通道音乐录音，展示人体身份/位置会改变可测声场；可用于研究房间指纹随人员和家具变化的鲁棒性 | 人是改变声场的被动目标，不是待定位的发声源；只有三个房间，不是跨房间源标签数据集 | [NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2023/hash/a4289154c9209b679ac761a50d5fec3a-Abstract-Datasets_and_Benchmarks.html) |
| 39 | A | SurroundSense: Mobile Phone Localization via Ambience Fingerprinting，MobiCom 2009 | 建立位置指纹数据库，将声音、光、颜色、Wi-Fi 和运动特征融合成逻辑位置标签；其“注册指纹—匹配测试样本”流程可直接改成源房间分类 | 手机位于被识别位置；声音只作为多模态过滤特征，单独使用不稳定；不是远端源房间识别 | [作者 PDF](https://sinrg.csl.illinois.edu/papers/surroundsense.pdf)、[DBLP](https://dblp.org/rec/conf/mobicom/AzizyanCC09) |
| 40 | B | Acoustic Non-Line-of-Sight Imaging，CVPR 2019 | 证明声波多次反射包含足够的隐藏空间信息，可以恢复拐角后的三维目标；支持“不要简单丢弃多径”的技术判断 | 主动发射调制 chirp，扫描多个扬声器/麦克风位置，目标是隐藏物体成像，不是被动喊叫定位 | [CVF](https://openaccess.thecvf.com/content_CVPR_2019/html/Lindell_Acoustic_Non-Line-Of-Sight_Imaging_CVPR_2019_paper.html) |
| 41 | B | Acoustic NLOS Imaging with Cross-Modal Knowledge Distillation，IJCAI 2023 | 用视觉教师向声学网络蒸馏隐藏场景结构知识，并在真实数据上提高噪声和未知物体条件下的 NLoS 重建性能 | 仍依赖主动声学三次反射测量；输出隐藏场景图像，不输出自然声源的房间标签 | [IJCAI](https://www.ijcai.org/proceedings/2023/156) |
| 42 | A | The Cone of Silence: Speech Separation by Localization，NeurIPS 2020 | 从多麦克风混合录音中同时定位并分离未知数量的说话人；适合作为“喊叫检测后，先按方向分离干扰人声”的前端 | 输出方位角而非房间；主要依赖空间方向信息，没有解决跨墙 NLoS 和多个房间共享同一门/走廊的问题 | [NeurIPS](https://proceedings.neurips.cc/paper/2020/hash/f056bfa71038e04a2400266027c169f9-Abstract.html) |
| 43 | B | Audio Location: Accurate Low-Cost Location Sensing，Pervasive 2005 | 系统讨论如何用普通麦克风、自然的人体发声（如弹指）做厘米级三维定位；为阵列部署、同步和 TDOA 设计提供经典基线 | 需要在空间中铺设多个麦克风；假设可检测的清晰声学事件，不研究跨墙声源或声学路径指纹分类 | [Microsoft Research](https://www.microsoft.com/en-us/research/publication/audio-location-accurate-low-cost-location-sensing/) |
| 44 | A | AdVerb: Visually Guided Audio Dereverberation，ICCV 2023 | 从混响音频和房间图像估计干净声音；其内容/环境分离思想可用于构造“内容无关、路径相关”的表示 | 原任务是去掉混响，而目标系统恰恰需要保留源房间和跨房间路径信息；必须改造成双分支或对比学习，不能直接使用其输出做房间指纹 | [CVF](https://openaccess.thecvf.com/content/ICCV2023/html/Chowdhury_AdVerb_Visually_Guided_Audio_Dereverberation_ICCV_2023_paper.html) |
| 45 | A | SoundSpaces: Audio-Visual Navigation in 3D Environments，ECCV 2020 | 在真实扫描户型中插入任意声源并渲染 RIR，明确展示声音经过门传播到另一房间；适合生成“卧室/厨房 → 固定客厅阵列”的预训练数据 | ECCV 是补充的 A 类视觉会议；仿真到真实住宅存在域差异，且原任务要求机器人移动导航 | [项目页](https://vision.cs.utexas.edu/projects/audio_visual_navigation/)、[ECVA PDF](https://www.ecva.net/papers/eccv_2020/papers_ECCV/papers/123510018.pdf) |
| 46 | A | RealMAN: A Real-Recorded and Annotated Microphone Array Dataset for Dynamic Speech Enhancement and Localization，NeurIPS 2024 Datasets and Benchmarks | 提供 32 通道阵列在 32 类真实场景中的静态/动态中文语音和真实噪声，可预训练稳健的定位、语音增强和可变阵列前端 | 不是多房间跨墙数据集；真实采集使用扬声器播放语音，标签是声源坐标而非源房间 | [NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2024/hash/bf8f6f5b017dc60d0c4e28a7a9a4ee7b-Abstract-Datasets_and_Benchmarks_Track.html) |

## 与目标系统的推荐组合

如果目标是“固定客厅阵列，从喊叫中判断卧室/厨房/卫生间”，推荐按以下方式组合，而不是寻找单篇论文直接复现：

1. 以 **37** 的真实家庭被动声源定位系统作为部署基线，先验证开放门、相连区域能否稳定映射到房间标签。
2. 以 **16 MAVL** 的 NLoS 多径分析替代只依赖直达声的传统 TDOA，处理墙后或另一房间人声。
3. 采用 **39** 的注册—匹配框架，但把注册对象从“麦克风所在位置指纹”改成固定接收端看到的 `source_room -> living_room` 路径指纹。
4. 采用 **42、46** 的多通道定位和真实噪声建模，得到方向、相位差和干净语音辅助特征。
5. 采用 **44** 的内容/环境分离思想，但保留环境分支；通过对比学习让同一房间的不同说话者、句子和位置聚集。
6. 使用 **45 / 29 SoundSpaces 2.0** 生成跨房间预训练样本，最终必须用目标住宅的真实录音校准。

建议模型不要只预测方向，而应学习如下联合表示：

```text
多通道喊叫录音
  -> 喊叫检测与分段
  -> Log-Mel/PCEN + IPD/GCC-PHAT + 多频带衰减/混响尾部
  -> 内容分支（说了什么、谁在说）
  -> 路径分支（源房间 -> 门/走廊/墙 -> 客厅阵列）
  -> source_room 分类 + unknown 拒识
```

## 未纳入正式编号的高相关论文

以下论文主题很接近，但 venue 不符合本批筛选条件，因此没有下载进 37–46：

- RevRIR，Interspeech 2024：从混响语音学习与 RIR 对齐的房间嵌入；
- Name That Room，ACM Multimedia 2012：从录音声学参数识别录制房间；
- Blind Estimation of the Reverberation Fingerprint，AES 2017：单麦克风盲估计混响指纹；
- SoundLoc，2014：RIR 房间分类，但正式记录为 CoRR/arXiv；
- Inferring Room Semantics Using Acoustic Monitoring，MLSP 2017：从语音盲分离 RIR 并判断房间语义。

这些论文可作为算法参考，但没有用来满足“顶会/A 类会议论文”的正式编号要求。
