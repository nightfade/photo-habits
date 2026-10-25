# photo-habits

递归扫描一个照片目录，从 EXIF 统计拍摄习惯：机身、镜头、焦距、光圈、ISO、快门、拍摄时间、横竖构图，以及长焦和 35mm 两个专题。每张照片还会按内容分成人像、街头、风景、城市建筑、动物、静物，对比不同题材下的拍摄习惯。

## 安装

整个工具只有一个脚本 `photo_habits.py`，任选一种方式拿到它：

```bash
git clone https://github.com/nightfade/photo-habits.git
cd photo-habits
```

或者只下载这一个文件：

```bash
curl -O https://raw.githubusercontent.com/nightfade/photo-habits/main/photo_habits.py
```

## 依赖

- Python 3.8+，只用标准库
- [exiftool](https://exiftool.org)
  - macOS：`brew install exiftool`
  - Windows：下载后把 `exiftool(-k).exe` 改名为 `exiftool.exe`，放进 PATH，或用 `--exiftool` 指定路径
- 可选：看图分类内容用的 CLIP 模型
  - `pip install open_clip_torch pillow`，HEIC 照片另装 `pillow-heif`
  - 首次运行下载约 600MB 模型；有 NVIDIA 显卡或 Apple 芯片会自动用上
  - 不装也能运行，内容分类只用关键词、目录名等元数据

## 用法

```bash
python3 photo_habits.py "照片根目录"
python3 photo_habits.py "照片根目录" --exclude-dir Export --exclude-dir 备份
python3 photo_habits.py --from-cache photo_habits_output/exif_cache.json   # 不重扫，只重算报告
python3 photo_habits.py --from-cache photo_habits_output/exif_cache.json --category 人像   # 整份报告只看人像
```

| 参数 | 作用 |
|---|---|
| `--out` | 输出目录，默认 `photo_habits_output/` |
| `--exclude-dir` | 跳过名字包含该字符串的目录，可重复 |
| `--include-exports` | 保留 Lightroom、Photoshop 等软件导出的 JPEG |
| `--include-phones` | 手机照片也进入习惯分析 |
| `--from-cache` | 读取之前保存的 exif_cache.json |
| `--content` | 内容分类方式：`auto`（默认，装了 CLIP 就看图）、`clip`（必须看图）、`meta`（只用元数据） |
| `--category` | 报告里机身之后的各节只统计这些类别，可重复 |

## 输出

| 文件 | 内容 |
|---|---|
| `report.md` | 统计报告 |
| `photos.csv` | 去重后每张照片一行，不含 GPS |
| `exif_cache.json` | exiftool 原始结果，供 `--from-cache` 重算 |
| `content_cache.json` | CLIP 看图结果，重算报告时不再看图 |

## 去重和过滤

- 同一张照片的 RAW + JPEG、多处备份只算一张，按机身 + 拍摄时间 + 文件名去重，优先保留 RAW
- 软件导出的 JPEG 默认排除
- 手机照片计入机身统计，默认不进习惯分析
- Bridge、Lightroom 写在 `.xmp` 旁车文件里的星级会合并进来

## 内容分类

每张照片按下面的顺序找类别，用第一个有结果的：

1. 关键词：Lightroom、Bridge 写进照片或 `.xmp` 的关键词，含层级关键词
2. 目录名：照片所在目录，越深的越优先
3. CLIP 看图：RAW 读内嵌预览图，不用解码整张 RAW
4. 相机场景模式（人像、风景）和人脸检测

关键词和目录名认这些词，中文按子串匹配，英文按整词匹配：

| 类别 | 词 |
|---|---|
| 人像 | 人像、肖像、写真、合影、孩子、宝宝、家人、portrait、people、family、kids、baby |
| 街头 | 街拍、街头、扫街、人文、street、candid |
| 风景 | 风景、风光、自然、landscape、scenery、nature |
| 城市建筑 | 建筑、城市、architecture、cityscape、urban、buildings |
| 动物 | 动物、鸟类、宠物、猫咪、狗狗、animal、wildlife、bird、pet、cat、dog |
| 静物 | 静物、美食、食物、微距、花卉、still life、food、macro、product |

- CLIP 判断最没把握的照片（最高类别概率低于 40%）归入「其他」
- 都判断不了的记为「未分类」
- 想改类别或词表，改脚本里的 `CATEGORIES`；改了 CLIP 提示词会自动重新看图

报告新增「照片内容」一节：

- 每个类别的张数、中位焦距和光圈、全开比例、ISO、快门、竖拍比例、留片率、最常用镜头
- 各类别的焦距、光圈、拍摄时段分布对比

## 报告里的两个专题

- **长焦**：把等效 70mm 以上的照片假设成只能用 f/5.6，按光圈差折算所需 ISO，并区分开大光圈是为了虚化还是为了进光
- **35mm 附近**：看等效 28–44mm 的照片里有多少用了比 f/2.8 更大的等效光圈

## 已知局限

- 相机时钟设错时区时，拍摄时段统计会偏
- 导出时裁切或缩放过的照片，等效焦距读数可能不准
- CLIP 分不清「主角是熟人的人像」和「街上的陌生人」，有人在画面里的街拍和环境人像会互相串；在公开示例图上约 86% 与人工标注一致
- 没装 CLIP 时，人脸检测会把有人的街拍也算成人像
- 旧版生成的 `exif_cache.json` 没有关键词和场景模式，`--from-cache` 时这两类依据用不上，重新扫描一次即可
