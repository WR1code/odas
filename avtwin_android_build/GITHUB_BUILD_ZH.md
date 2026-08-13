# 使用 GitHub Actions 编译 APK

本源码包已经包含 `.github/workflows/build-avtwin-apk.yml`。

1. 将源码包解压到 GitHub 仓库根目录，保留 `avtwin_android_build/` 和 `.github/` 两个目录。
2. 提交并推送到 GitHub。
3. 打开仓库的 **Actions** 页面，选择 **Build AV-Twin Android APK**。
4. 点击 **Run workflow**，等待测试和编译完成。
5. 在该次运行页面底部下载
   `AVTwinAndroidResponder-v0.9.0-ack-pose-debug-apk` artifact。

工作流使用 Java 17、Gradle 8.13、Android SDK 36 和 Build Tools 36.0.0，先运行单元测试，
测试通过后才会生成 debug APK。
