# 顶会声学论文库

本目录目前收录 36 篇英文原版论文：01–15 为原有房间声学/RIR 语料，16–36 为围绕“家庭喊叫识别、跨房间 NLoS 声源定位、房间级声音事件感知、音视导航与仿真”的扩展语料。

- `originals/`：会议官网、arXiv 或作者公开页面提供的英文原版 PDF。
- `originals/RESEARCH_GUIDE_16_36.md`：新增论文的相关性分级、适用边界、公开链接、已核实引用链和建议阅读顺序。
- `bilingual_zh_en/`：中英文对照 PDF。02、03、05–08 采用带边框的逐段双栏对照版式；05–08 在正文首次引用某图后紧接原图及对应双语图注。
- `build_bilingual.py`：分页式中英文 PDF 的可断点续跑生成脚本；翻译缓存位于 `.translation_cache/`。
- `build_boxed_bilingual.py`：05–08 逐段双栏带框版的重建脚本；段落翻译缓存位于 `.boxed_translation_cache/`。

## 译文说明

中文内容为机器辅助全文翻译，适合检索、通读和与英文逐页核对，不应替代论文原文用于严谨引用。双栏读取顺序由 `pdftotext` 自动解析，少数图注、公式或表格单元可能发生顺序错位；逐段版标注了对应的原论文页码，严谨引用及图表核对请以 `originals/` 中的英文原版为准。

## 公开来源

1. Deep Room Recognition Using Inaudible Echos — arXiv 1809.00531
2. Measuring Acoustics with Collaborative Multiple Agents — IJCAI 2023 Proceedings
3. Deep Neural Room Acoustics Primitive — PMLR / ICML 2024
4. Few-Shot Audio-Visual Learning of Environment Acoustics — NeurIPS 2022 Proceedings
5. Learning Neural Acoustic Fields — NeurIPS 2022 Proceedings
6. Acoustic Volume Rendering for Neural Impulse Response Fields — NeurIPS 2024（复用项目中已有原版）
7. Hearing Anything Anywhere — CVF Open Access / CVPR 2024
8. Hearing Anywhere in Any Environment — CVF Open Access / CVPR 2025
9. Resounding Acoustic Fields with Reciprocity — NeurIPS 2025 Proceedings
10. Real Acoustic Fields — CVF Open Access / CVPR 2024
11. AV-RIR — CVF Open Access / CVPR 2024
12. Differentiable Room Acoustic Rendering with Multi-View Vision Priors — CVF Open Access / ICCV 2025
13. Smartphone-based Acoustic Indoor Space Mapping — 作者公开 PDF
14. BatMapper — 作者公开 PDF
15. EchoTag — ACM SIGMOBILE 公开 PDF
16. MAVL: Multiresolution Analysis of Voice Localization — NSDI 2021 / USENIX
17. Symphony: Localizing Multiple Acoustic Sources with a Single Microphone Array — SenSys 2020 / 作者公开 PDF
18. Voice Localization Using Nearby Wall Reflections (VoLoc) — MobiCom 2020 / 作者公开 PDF
19. Indoor Localization without Infrastructure Using the Acoustic Background Spectrum — MobiSys 2011 / 作者公开 PDF
20. SoundSense — MobiSys 2009 / 作者公开 PDF
21. Ubicoustics — UIST 2018 / 作者公开 PDF
22. HomeSound — CHI 2020 / 作者公开 PDF
23. ProtoSound — CHI 2022 / 作者公开 PDF
24. SPECTRA — CHI 2025 / 作者公开 PDF
25. Semantic Audio-Visual Navigation — CVPR 2021 / CVF Open Access
26. Learning to Set Waypoints for Audio-Visual Navigation — ICLR 2021 / arXiv（OpenReview 条目核验）
27. Sound Adversarial Audio-Visual Navigation — ICLR 2022 / 作者公开 PDF（OpenReview 条目核验）
28. Audio-Visual Floorplan Reconstruction — ICCV 2021 / CVF Open Access
29. SoundSpaces 2.0 — NeurIPS 2022 / NeurIPS Proceedings
30. Sound Localization from Motion — ICCV 2023 / CVF Open Access
31. Self-Supervised Visual Acoustic Matching — NeurIPS 2023 / NeurIPS Proceedings
32. Visual Acoustic Matching — CVPR 2022 / CVF Open Access
33. Image2Reverb — ICCV 2021 / CVF Open Access
34. Novel-View Acoustic Synthesis — CVPR 2023 / CVF Open Access
35. Be Everywhere – Hear Everything (BEE) — ICCV 2023 / CVF Open Access
36. WALRUS — MobiSys 2005 / USENIX
