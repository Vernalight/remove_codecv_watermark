# CodeCV Resume Watermark Remover

一个用于移除 CodeCV 导出简历 PDF 平铺水印的小脚本。

脚本会删除 PDF 内容流中的水印绘制指令（旧版 `/Pattern` 平铺填充，或新版整页水印图片 XObject），不会把 PDF 转成图片再涂白，因此可以尽量保留原始文本、头像、排版和页面尺寸。

## 功能

- 移除 CodeCV 导出 PDF 中的 `CodeCV简历` 平铺水印
- 支持两代水印实现：
  - **矢量平铺（旧版）**：`/Pattern` 颜色空间填充指令
  - **整页水印图（新版）**：铺满整页、带透明蒙版（SMask）的水印图片 XObject，通过 CTM 追踪识别"覆盖 ≥90% 页面的透明图"后整体移除
- 保留原 PDF 页数、页面尺寸和可抽取文本
- 支持命令行指定输入、输出路径
- 如果不指定输出路径，会自动生成 `*.clean.pdf`

## 安装

需要 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

也可以直接使用已有 Python 环境安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 使用

指定输出文件：

```bash
python3 remove_codecv_watermark.py "带水印简历.pdf" "去水印简历.pdf"
```

不指定输出文件时，默认输出到同目录的 `.clean.pdf` 文件：

```bash
python3 remove_codecv_watermark.py "带水印简历.pdf"
```

运行成功后会看到类似输出：

```text
Removed 0 pattern fill(s) and 2 watermark image(s).
Wrote: 带水印简历.clean.pdf
```

## 快速检查

```bash
python3 remove_codecv_watermark.py --help
```

如果能正常显示帮助信息，说明依赖安装和脚本入口正常。

## 注意事项

- 本脚本针对 CodeCV 导出 PDF 的两代水印实现（矢量 `/Pattern` 平铺、整页栅格水印图）。
- 新版水印图的判定条件：图片带透明蒙版（`/SMask`）且绘制范围覆盖 ≥90% 页面，普通头像、Logo 等小图不受影响。
- 如果 CodeCV 后续改变导出实现，脚本可能需要调整。

