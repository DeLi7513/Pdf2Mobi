# pdf2mobi for Android

把 PDF 转成 MOBI / EPUB 的安卓应用。**自动识别文字版还是扫描版**，文字优先，扫描版用 OCR 识别成文字。

---

## 怎么得到 APK

我在 Windows 上无法直接编译 APK（buildozer 只支持 Linux），所以用**云端自动构建**：你只要把项目传到 GitHub，点一下按钮，十几分钟后下载 APK。

### 步骤

1. **新建一个 GitHub 仓库**（可以是私有仓库），把 `pdf2mobi-android` 文件夹里的**全部内容**上传进去。
2. 打开仓库的 **Actions** 标签页，左侧选 **Build Android APK**。
3. 点右侧 **Run workflow** → **Run workflow**。
4. 等大约 15～25 分钟（首次会慢一些，之后会走缓存）。
5. 构建完成后，点进那次运行，在页面底部 **Artifacts** 里下载 **pdf2mobi-apk**。
6. 解压得到 `pdf2mobi-1.0.0-debug.apk`，传到手机上安装。

> 手机安装时可能提示「未知来源」，需要在系统设置里允许安装未知应用。
> 这是 debug 版 APK，用来自己安装完全够用；要上架应用商店才需要签名版。

---

## 应用怎么用

1. **添加 PDF** — 调起系统文件选择器，可多选
2. 列表里显示每个文件的**类型**：文字版 / 扫描版 / 混合版
3. 顶部选**输出格式**（MOBI / EPUB / TXT）
4. **OCR** 按钮切换开关（默认开启）
5. 点 **开始转换**

转换在后台进行，界面不会卡。每个文件都会显示状态（排队中 / 转换中 / 完成 / 失败），底部有日志。

- **分析类型** — 先看清每个 PDF 是什么类型再决定怎么转
- **清空** — 移除所有文件
- 文件行右侧的 **✕** — 移除单个文件
- **更改** — 换输出目录（默认存到手机存储的 `pdf2mobi` 文件夹）

### OCR 说明

扫描版 PDF 没有文字层，必须靠 OCR 识别。应用调用**安卓自带的 ML Kit**（谷歌的文字识别），中文识别效果好，而且**几乎不增加安装包体积**——语言模型由系统按需下载。

首次对扫描版做 OCR 时，系统可能提示下载中文识别模型，**需要联网**。下载一次后就能离线用了。

如果 OCR 不可用（比如机型没有 Google 服务），程序**不会崩溃**：会提示「无文字可提取」并跳过该文件，文字版 PDF 照常处理。

---

## 和桌面版的区别

| | 桌面版 | 安卓版 |
|---|---|---|
| PDF 解析 | PyMuPDF（快） | pypdf（纯 Python） |
| 图片 | 可保留 | **不保留**（按你的要求） |
| 扫描版 | 整页嵌图 | **OCR 识别成文字** |
| OCR | 无 | ML Kit |

### 为什么安卓版换了解析库

PyMuPDF 功能强、速度快，但**没有安卓版本**——python-for-android 没有它的编译配方，PyPI 上也没有 aarch64 安卓轮子。所以安卓端改用 **pypdf**，它是**纯 Python**，可以原样在安卓上运行。

代价是慢一些，但一本书只解析一次，而且界面有进度提示，实际用起来没问题。

同理没用 pdfminer，因为它依赖 `cryptography`（原生库，没有安卓配方）；也没用 tesseract OCR，同样没有配方。

---

## 已验证的部分

转换核心是**纯 Python**，所以在电脑上就能完整测试，**56 项自测全部通过**：

```
python selftest.py
```

覆盖：

| 项目 | 说明 |
|---|---|
| **编码修复** | 27 个用例，确保英文/拉丁文/中文/日文/韩文都正确处理 |
| **MOBI 字节编解码** | ASCII / 中文 / emoji / 全 256 字节，往返无损 |
| **完整流程** | 文字版、混合版、扫描版的 MOBI + EPUB 产物校验 |
| **中文 PDF** | 标题、正文、目录页码全部正确 |
| **OCR 接口** | 钩子正确调用、文字正确保留、不可用时优雅降级 |
| **产物结构** | PalmDB 头、MOBI 头、EXTH、记录偏移、EPUB XML 合法性 |

### 测试中发现并修复的两个真实 bug

1. **中文全部乱码**。pypdf 不认识 PDF 里常用的 `UniGB-UTF16-H` 这类预定义 CMap，导致 `第一章` 被解成 `{,N`。修复方式是识别字节被压平的特征并还原成 UTF-16。

2. **编码修复会毁掉英文书**。第一版修复用「CJK 字符变多就算修好」判断，结果 `Hello world` 被"修复"成 `䡬汬漠睯牬`。这个 bug 会**静默损坏每一本英文书**。修复后改用字符分布判断（真正的文本 vs 噪声），并加了非 ASCII 要求。

---

## 自己构建（可选）

如果你有 Linux 环境或 WSL，也可以本地构建：

```bash
sudo apt install -y git zip openjdk-17-jdk autoconf libtool pkg-config \
                    zlib1g-dev cmake libffi-dev libssl-dev build-essential
pip install "buildozer==1.5.0" "cython<3.0"
buildozer -v android debug
```

APK 会出现在 `bin/` 目录。

---

## 项目结构

```
pdf2mobi-android/
├── main.py                     # buildozer 要求的入口
├── buildozer.spec              # 打包配置
├── requirements.txt            # 运行依赖（都是纯 Python）
├── selftest.py                 # 自测（56 项）
├── src/pdf2mobi_android/
│   ├── pdftext.py              # pypdf 文本提取、分类、目录解析
│   ├── encoding_fix.py         # CJK 编码修复（关键）
│   ├── builder.py              # 重建段落/标题、组装书籍
│   ├── writers.py              # MOBI / EPUB 写出器（纯 Python）
│   ├── ocr.py                  # ML Kit OCR 桥接
│   └── ui.py                   # Kivy 界面
├── android-src/org/pdf2mobi/
│   └── OcrBridge.java          # ML Kit 的 Java 桥
└── .github/workflows/
    └── android.yml             # 云构建配置
```

---

## 已知限制

- **只有 debug APK**（云构建默认产物）。自用没问题，上架商店需要签名。
- **OCR 需要联网下载一次语言模型**，之后可离线用。
- 加密 PDF 会被跳过，需要先去掉密码。
- 复杂多栏排版、公式、表格的还原是 PDF 转换的固有难点。
- 首次构建较慢（要下载 Android SDK/NDK），之后有缓存会快很多。

---

## 关于「无损」的说明

PDF 是固定版面格式，MOBI 是可重排格式，**严格无损转换不可能**。本应用做到的是：

- **文字版** → 内容零丢失，可重排、可搜索、可查词
- **扫描版** → OCR 成文字（识别率取决于原件清晰度），**原始版面不保留**

你说了「图片不做保留要求」，所以扫描页的原始图像不会嵌入——这也让产物体积小很多（一本书通常几百 KB 而不是几十 MB）。
