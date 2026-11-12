# Image Library

简体中文 | [English](#english)

一个面向 Windows 的本地优先图片整理与浏览工具。原图始终留在自己的目录中，索引、缩略图、评分、相册和用户元数据保存在本地 SQLite 数据库。页面由 [LocalDock](https://github.com/pmmxd/localdock) 托管，不依赖云服务。

本仓库只包含程序源码、第三方前端资源和文档，不包含照片、数据库、缩略图、地图缓存或本机路径。

## 功能

- 支持普通照片与多种 RAW 格式。
- 按日期、评分、标签、相机、镜头和地点筛选。
- 提供全部、精选、时间线、地图和纯图瀑布流视图。
- 从图片中读取相机、镜头、GPS、评分和关键词等 ExifTool 元数据。
- 使用 ExifTool 写回评分、说明、关键词和 GPS；支持单张或批量操作。
- 支持普通相册和按筛选条件自动匹配的智能相册，相册数据持久化到 `相册.db`。
- 支持批量设置标签、评分、GPS、精选，或将照片移入 Windows 回收站。
- 支持增量更新和完整重建索引。
- 高德地图瓦片首次获取后缓存在本地，后续可复用。
- 原图默认由 Windows 软件打开，不移动、不重命名。

## 支持的格式

常规图片：

```text
JPG, JPEG, PNG, WebP, AVIF, HEIC, TIFF, TIF, BMP, GIF
```

RAW：

```text
CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2, PEF, SRW
```

RAW 显示效果取决于 Pillow 解码能力和系统中的 RAW 预览支持；原始文件不会被转换或替换。

## 运行要求

- Windows 10 或 Windows 11
- Python 3.11 或更高版本
- Pillow
- [ExifTool](https://exiftool.org/) 已加入 `PATH`
- [LocalDock](https://github.com/pmmxd/localdock) 源码版或绿色版

先验证 ExifTool：

```powershell
exiftool -ver
```

安装 Python 依赖：

```powershell
python -m pip install Pillow
```

## 快速开始

### 1. 获取 LocalDock

从 [pmmxd/localdock](https://github.com/pmmxd/localdock) 获取源码或绿色版，并确认 `LocalDock.exe` 可用。LocalDock 的安装与使用方式以其仓库 README 为准。

### 2. 获取 Image Library

```powershell
git clone https://github.com/pmmxd/image-library.git
cd image-library
```

仓库可以放在任意位置，不需要放进图片目录。

### 3. 指定图片库并启动

```powershell
python .\start_library.py `
  --root "D:\Photos" `
  --localdock "C:\path\to\LocalDock\LocalDock.exe"
```

参数说明：

- `--root`：要扫描的图片库根目录。程序不会把照片复制到仓库中。
- `--localdock`：`LocalDock.exe` 的完整路径。也可以设置 `LOCALDOCK_EXE` 环境变量后省略该参数。
- `--no-browser`：启动后不自动打开浏览器。

启动脚本会完成以下操作：

1. 在图片库根目录下创建隐藏的 `.media-library` 工作目录。
2. 启动本地图片后端 `http://127.0.0.1:18766`。
3. 首次运行时扫描图片并生成索引、缩略图和静态页面。
4. 在 LocalDock 中注册并启动该图片库站点。
5. 打开 LocalDock 分配的本地地址。

如果项目已经位于 `<图片库根目录>\.media-library\app`，可以省略 `--root`。

### 4. 使用批处理启动

先设置两个环境变量：

```powershell
$env:MEDIA_LIBRARY_ROOT = "D:\Photos"
$env:LOCALDOCK_EXE = "C:\path\to\LocalDock\LocalDock.exe"
```

再双击：

```text
启动图片库.bat
```

也可以在当前 PowerShell 会话中运行同一个批处理。

## LocalDock 集成

Image Library 负责索引图片和提供本地 API；LocalDock 负责托管生成的 `图片浏览.html`，并按需提供以下桌面能力：

- 使用默认软件打开原图。
- 在资源管理器中定位文件。
- 将照片安全地移入 Windows 回收站。

`start_library.py` 会通过 LocalDock 的本地管理 API 自动创建或更新站点。站点名称类似“图片库 - Photos”，端口由 LocalDock 自动分配。

LocalDock 默认管理地址为 `http://127.0.0.1:7080/`。如果 LocalDock 没有运行，并且 `--localdock` 指向有效的 `LocalDock.exe`，启动脚本会尝试启动它。

## 数据目录

运行时数据位于目标图片库中，而不是 Git 仓库中：

```text
<图片库根目录>/
  .media-library/
    data/
      图片索引.db
      相册.db
      图片浏览.html
      图片元数据.json
      图片元数据.csv
      相册.json
      缩略图/
      地图缓存/
  照片和子目录...
```

说明：

- `图片索引.db` 保存照片索引和用户元数据。
- `相册.db` 保存普通相册和智能相册，是最重要的用户分类数据之一。
- `相册.json` 是旧版兼容备份，页面以 `相册.db` 为主。
- `缩略图` 和 `地图缓存` 可以重新生成。
- `图片索引.db` 和 `相册.db` 不能仅靠重新扫描照片完整恢复，迁移或大改前应备份整个 `.media-library\data`。

仓库的 `.gitignore` 已排除数据库、照片导出、缩略图、缓存和本地工具数据。

## 隐私与网络

- 照片、数据库和缩略图默认只在本地处理，不上传到本项目。
- 地图视图会从高德地图请求地图瓦片；程序本地缓存已获取的瓦片。
- 使用“输入城市或景点查询 GPS”时，输入的文本会发送到 Photon 地理编码服务。
- 不打开地图或地点查询时，图片整理功能不依赖远程服务。
- 公开仓库不包含任何用户的照片、数据库、GPS 或相册分类。

## 开发说明

主要文件：

```text
start_library.py          # 启动后端并注册 LocalDock 站点
image_server.py           # 本地 HTTP API、文件访问和元数据写回
build_image_library.py    # 扫描图片、生成索引、缩略图和 HTML
library_db.py             # SQLite 结构与元数据操作
albums_db.py              # 独立相册数据库
vendor/                   # Leaflet 等前端资源
使用说明.md               # 更详细的中文操作说明
```

基本语法检查：

```powershell
python -m py_compile .\*.py
```

## 许可证

本仓库当前未附加开源许可证。第三方组件仍遵循其各自许可证，见 `vendor/` 中的文件说明。

## English

A local-first Windows image organizer and browser. Original photos stay in place, while indexes, thumbnails, ratings, albums, and user metadata are stored in local SQLite databases. The generated web page is served by [LocalDock](https://github.com/pmmxd/localdock); no cloud account is required.

This repository contains source code, third-party frontend assets, and documentation only. It does not contain photos, databases, thumbnails, map caches, or machine-specific paths.

### Features

- Browse common image formats and a broad range of camera RAW files.
- Filter by date, rating, tag, camera, lens, and location.
- Use All, Featured, Timeline, Map, and image-only masonry views.
- Read camera, lens, GPS, rating, and keyword metadata through ExifTool.
- Write ratings, descriptions, keywords, and GPS back to supported image metadata.
- Create persistent manual and smart albums.
- Batch-edit tags, ratings, GPS, featured state, or move files to the Windows Recycle Bin.
- Run incremental or full index rebuilds.
- Cache AMap tiles locally after the first request.

### Requirements

- Windows 10 or Windows 11
- Python 3.11+
- Pillow
- ExifTool available on `PATH`
- [LocalDock](https://github.com/pmmxd/localdock), either from source or as a portable build

```powershell
python -m pip install Pillow
exiftool -ver
```

### Quick Start

```powershell
git clone https://github.com/pmmxd/image-library.git
cd image-library
python .\start_library.py `
  --root "D:\Photos" `
  --localdock "C:\path\to\LocalDock\LocalDock.exe"
```

The repository can live anywhere. `--root` points to the image library that should be indexed, and `--localdock` points to `LocalDock.exe`. You can also set `LOCALDOCK_EXE` instead of passing `--localdock`.

On startup, the script:

1. Creates a hidden `.media-library` workspace under the image library root.
2. Starts the local API at `http://127.0.0.1:18766`.
3. Scans images and generates the index, thumbnails, and HTML page on first run.
4. Registers and starts the generated site through LocalDock.
5. Opens the local URL assigned by LocalDock.

If the source is located at `<library root>\.media-library\app`, `--root` can be omitted.

### LocalDock Integration

Image Library owns indexing and the local API. LocalDock serves the generated `图片浏览.html` and provides desktop operations such as opening originals, revealing files in Explorer, and moving files to the Recycle Bin.

LocalDock's default manager URL is `http://127.0.0.1:7080/`. If it is not running and `--localdock` points to a valid executable, the startup script attempts to launch it.

### Runtime Data

```text
<image library root>/
  .media-library/
    data/
      图片索引.db
      相册.db
      图片浏览.html
      图片元数据.json
      图片元数据.csv
      相册.json
      缩略图/
      地图缓存/
  photos and subdirectories...
```

`图片索引.db` stores the media index and user metadata. `相册.db` stores manual and smart albums. `相册.json` is a legacy compatibility backup. Thumbnails and map tiles can be regenerated, but the two databases contain user-created organization and should be backed up before migration.

All runtime data is excluded by `.gitignore`.

### Privacy and Network Access

- Photos and databases are processed locally and are not uploaded by this project.
- The map view fetches tiles from AMap and caches them locally.
- City or landmark lookup sends the entered query to the Photon geocoding service.
- ExifTool writes metadata only when you explicitly save or batch-edit metadata.
- The public repository contains no user media, databases, GPS data, or album data.

### License

No open-source license is currently attached to this repository. Third-party components remain under their respective licenses; see the notices under `vendor/`.
