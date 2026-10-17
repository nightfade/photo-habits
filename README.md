# photo-habits

递归扫描一个照片目录，从 EXIF 统计拍摄习惯：机身、镜头、焦距、光圈、ISO、快门、拍摄时间、横竖构图，以及长焦和 35mm 两个专题。

## 依赖

- Python 3.8+，只用标准库
- [exiftool](https://exiftool.org)
  - macOS：`brew install exiftool`
  - Windows：下载后把 `exiftool(-k).exe` 改名为 `exiftool.exe`，放进 PATH，或用 `--exiftool` 指定路径

## 用法

```bash
python3 photo_habits.py "照片根目录"
python3 photo_habits.py "照片根目录" --exclude-dir Export --exclude-dir 备份
python3 photo_habits.py --from-cache photo_habits_output/exif_cache.json   # 不重扫，只重算报告
```

| 参数 | 作用 |
|---|---|
| `--out` | 输出目录，默认 `photo_habits_output/` |
| `--exclude-dir` | 跳过名字包含该字符串的目录，可重复 |
| `--include-exports` | 保留 Lightroom、Photoshop 等软件导出的 JPEG |
| `--include-phones` | 手机照片也进入习惯分析 |
| `--from-cache` | 读取之前保存的 exif_cache.json |

## 输出

| 文件 | 内容 |
|---|---|
| `report.md` | 统计报告 |
| `photos.csv` | 去重后每张照片一行，不含 GPS |
| `exif_cache.json` | exiftool 原始结果，供 `--from-cache` 重算 |

## 去重和过滤

- 同一张照片的 RAW + JPEG、多处备份只算一张，按机身 + 拍摄时间 + 文件名去重，优先保留 RAW
- 软件导出的 JPEG 默认排除
- 手机照片计入机身统计，默认不进习惯分析
- Bridge、Lightroom 写在 `.xmp` 旁车文件里的星级会合并进来

## 报告里的两个专题

- **长焦**：把等效 70mm 以上的照片假设成只能用 f/5.6，按光圈差折算所需 ISO，并区分开大光圈是为了虚化还是为了进光
- **35mm 附近**：看等效 28–44mm 的照片里有多少用了比 f/2.8 更大的等效光圈

## 已知局限

- 相机时钟设错时区时，拍摄时段统计会偏
- 导出时裁切或缩放过的照片，等效焦距读数可能不准
