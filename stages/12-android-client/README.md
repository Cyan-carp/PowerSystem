# 源网智联 · 安卓客户端（WebView 外壳）

把平台网页装进一个安卓 App：原生只负责外壳，界面仍是阶段四的前端页面。
与第五版 Windows 桌面客户端（`stages/11-desktop-client`）同一思路——**不复制一份界面**，
只提供壳层与系统能力，网页更新后 App 无需重新发版。

```
stages/12-android-client/
├── app/
│   ├── AndroidManifest.xml              # 单 Activity、minSdk 24 / targetSdk 34
│   ├── src/com/powersystem/mobile/
│   │   └── MainActivity.java            # 全部外壳逻辑（无 R 引用、无第三方依赖）
│   └── res/
│       ├── values/                      # 文案、主题色、无 ActionBar 主题
│       ├── drawable/ic_launcher_foreground.xml
│       ├── mipmap-anydpi-v26/ic_launcher.xml   # 自适应图标（Android 8+）
│       └── xml/shortcuts.xml            # 长按图标的「服务器设置」入口
├── scripts/
│   ├── make-icons.ps1                   # 用 System.Drawing 生成各密度启动图标 PNG
│   └── build-apk.ps1                    # aapt2 → javac → d8 → zipalign → apksigner
└── build/                               # 构建产物与签名密钥（git 忽略）
    ├── keys/powersystem-mobile.jks
    └── out/PowerSystem-Android-<版本>.apk
```

## 构建

前置：Android SDK（`ANDROID_HOME` 或 `%LOCALAPPDATA%\Android\Sdk`）+ JDK 17 以上
（Android Studio 自带的 `jbr` 即可，脚本会自动找）。**不需要 Gradle，也不联网拉依赖。**

```powershell
powershell -ExecutionPolicy Bypass -File stages/12-android-client/scripts/build-apk.ps1
# 换版本号
powershell -ExecutionPolicy Bypass -File stages/12-android-client/scripts/build-apk.ps1 -VersionName 1.0.1 -VersionCode 2
```

产物：`stages/12-android-client/build/out/PowerSystem-Android-1.0.0.apk`（约 0.1 MB）。

> 脚本用 UTF-8 **带 BOM** 保存：Windows PowerShell 5.1 默认按 GBK 读 `.ps1`，
> 去掉 BOM 会让中文提示变成乱码并导致解析失败。
>
> 中间产物落在 `%TEMP%\powersystem-apk-build`（纯 ASCII 路径）：`aapt2`/`zipalign`
> 是原生程序，打不开仓库里的中文路径；最终 APK 由 Java 工具写回仓库目录。

## 安装

```powershell
adb install -r stages/12-android-client/build/out/PowerSystem-Android-1.0.0.apk
```

或把 APK 传到手机后直接点击安装（需允许「安装未知来源应用」）。

### 签名密钥

首次构建会在 `build/keys/powersystem-mobile.jks` 自动生成自签名密钥
（别名 `powersystem`，口令 `powersystem`，仅用于内部安装包）。

- **这个文件不能丢**：换密钥后新包无法覆盖安装，只能先卸载（会清掉本地登录态）。
- 目录已被 `.gitignore` 排除，不进仓库。要对外分发，请换自己的密钥并妥善保管。
- 包名 `com.powersystem.mobile` 与签名共同决定"能否覆盖升级"，改包名等于另一个应用。

## 首次启动

1. 打开 App → 显示「服务器地址」，默认 `https://www.cyancarp.cn/`。
2. 需要连内网/测试环境时改成对应地址（例如 `http://192.168.1.10:5173/`），点「保存并进入」。
3. 之后进入平台自带的登录页，登录态与网页共用（同一 WebView Cookie / localStorage）。

**以后想改地址**：长按桌面上的应用图标 → 「服务器设置」（静态快捷方式，需 Android 8+）。
连不上时错误页也有「修改地址」按钮。

## 外壳能力与边界

| 能力 | 行为 |
| --- | --- |
| 站内链接 | 留在 WebView，保留会话与历史 |
| 站外链接 | 交给系统浏览器，不在 App 内开无壳页面 |
| 返回键 | 先退网页历史；到底后连按两次退出 |
| 下载（报表 CSV） | 交给系统下载器落到「下载」目录，并带上登录 Cookie |
| 软键盘 | `adjustResize`：助手页输入框不被键盘遮挡 |
| 旋转屏幕 | `configChanges` 接管，不重建 WebView（不丢会话） |
| 缩放 | 关闭手势缩放，布局交给网页 viewport 与响应式断点 |
| 混合内容 | `NEVER_ALLOW`：HTTPS 页面不加载 HTTP 资源 |
| 本地文件 | `allowFileAccess=false`、`allowContentAccess=false` |

`usesCleartextTraffic="true"` 是为了能连内网 http 测试环境；生产入口是 HTTPS，
若确定只用生产，可在 `AndroidManifest.xml` 里改成 `false` 收紧。

## 与网页的配合

手机端布局由阶段四前端负责，断点与规则见 `stages/04-frontend/README.md` 的「手机端」一节：
≤900px 隐藏左侧图标轨，改为底部四格导航 + 「更多」面板。**App 本身不含任何界面代码**，
网页改了手机端布局，App 直接跟着变。

设计与验收记录见第八版笔记：
`notes/01-项目调研规划与开发建设记录/08-第八版-移动端与安卓客户端/00-索引·第八版-移动端与安卓客户端.md`
（选型与安全边界、S02 四件套、构建链五个坑）。

## 已验证范围

- 用 Android 16（API 36）模拟器 `Medium_Phone_API_36.1`（1080×2400 / 420dpi）实装：
  首次启动设置页 → 指向本地 dev 服务 → 登录 → 总览/告警页 → 「更多」面板 → 返回键，
  截图与 logcat 未见崩溃或报错。
- `aapt2 dump badging`：包名、版本、minSdk 24 / targetSdk 34、权限、中文应用名与自适应图标均正确。

未验证：真实手机型号、安卓 7–10 老系统、外网弱网切换、系统下载器落盘（受模拟器限制未实测）。
