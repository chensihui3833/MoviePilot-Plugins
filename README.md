# 蓝光原盘分集（BlurayEpisodeSplit）

MoviePilot 插件。接管蓝光原盘电视剧的整理，解析 `BDMV/PLAYLIST/*.mpls`，判定每张盘里有几集、哪个 `.m2ts` 对应第几集，然后**按集拆成独立目录**落地，每个目录内含该集自己的 BDMV 结构。

## 解决什么问题

MoviePilot 原生的蓝光整理逻辑只会把整个 `DISC1` 目录按盘号重命名挂到媒体库，**不会拆集**。于是 Emby/Jellyfin 看到的是一整张盘，不是 10 集电视剧。

本插件在整理链上接管 BDMV 盘目录，把它拆成 `剧名 - S01E01`、`剧名 - S01E02` …… 这样的逐集目录。

## 判定原理

只读 MPLS 播放列表，不依赖菜单脚本、不依赖 OCR、不依赖文件名规律。

1. **排除假 play-all**：整盘合集的播放列表 `playitem_count > 1`，直接排除。这是唯一能排除它的判据——实测某盘存在 `00096.mpls`（101 个 playitem，77.6 分钟）与 `00099.mpls`（100 个 playitem），时长都落在正片区间内，只看时长无法区分。
2. **按时长窗口筛正片**：默认 `min_episode_seconds=600`（10 分钟）到 `max_episode_seconds=4500`（75 分钟）。实测三盘时长直方图在 [600, 1800) 区间为空，所以下限取 600 秒是安全的；上限取 1200 秒会漏掉 25 分钟的短剧，因此取 75 分钟。
3. **按 clip 区间去重**：同一集常有 3 个等长时间副本（例如 `00335.mpls` / `00339.mpls` / `00343.mpls` 指向同一组 clip）。不去重会得到 12/12/6 集而非真实的 4/4/2 集。去重键是 **clip 名 + in/out 时间点**，不是 clip 名：同一集重复三条播放路径时区间逐字节相同，而"一个 clip 切成两集"必须靠区间区分，只看 clip 名会把两集错并成一集。
4. **集序按 STREAM 里 m2ts 的写入时间排**：蓝光制作按播放顺序写 STREAM，mtime 是制作顺序的原始证据。**按时长排序是错的**——实测某盘四集时长 3890.9 / 2951.9 / 2526.5 / 2561.6 前三后四倒挂，按时长会把第 3、4 集排反。mtime 同时不受 clip 编号不连续影响：实测某盘第 4 集是 clip `00348`（编号最大却排最后）。无 STREAM 或 stat 失败时退回按 clip 名排序。
5. **花絮用时长下限识别**：默认 `min_bonus_seconds=300`（5 分钟）。不能靠"未被任何 playlist 引用"识别——实测三盘孤儿 `.m2ts` 均为 0 个，且 150+ 个 clip 只被 6 秒的菜单 playlist 引用。阈值扫描结果：0 秒→38/30/27、60 秒→9/3/4、180 秒→3/1/4、**300 秒→2/0/3（真值）**、600 秒→0/0/0，300 秒是唯一在三张盘上同时命中真值的值。
6. **季号来源**：调用方传入 > 盘内元数据（`bdmt_*.xml`）> 兄弟盘唯一季号 > 0（不确定就不写死）。

> `Episode.playlist` 记录的是**实际命中的播放列表文件名**，不是从 clip 名反推的。实测某张盘第 4 集是 `00362.mpls` 指向 clip `00348`；`00348.mpls` 只是一个 20 秒花絮。

## 每集产物形态

```
剧名 (年份)/
  Season 1/
    剧名 (2016) - E01/
      BDMV/
        STREAM/     该集自己的 .m2ts（及同名字幕/音轨）
        CLIPINF/    对应的 .clpi
        PLAYLIST/   该集用到的 .mpls
        META/       从源盘递归复制
        index.bdmv  MovieObject.bdmv
      CERTIFICATE/  从源盘递归复制
```

花絮（`bonus_handling`）：

- `attach`（默认）：花絮的文件追加进该盘的首个分集目录
- `separate`：单独建一个 `剧名 - Bonus N` 目录
- `skip`：不处理花絮

整理方式是**硬链接**，不占额外空间。

## 配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 总开关 |
| `notify` | bool | true | 整理后是否通知 |
| `min_episode_minutes` | number | 10 | 正片时长下限（分钟） |
| `max_episode_minutes` | number | 75 | 正片时长上限（分钟） |
| `min_bonus_minutes` | number | 5 | 花絮时长下限（分钟） |
| `episode_offset` | number | 0 | 集号偏移，用于接续前一季或修正起点 |
| `bonus_handling` | select | attach | attach / separate / skip |
| `dry_run` | bool | false | 只算不搬，用于试跑 |

## 兼容性

- **MoviePilot v2 与 v3 均可**：只用 `from app.plugins import _PluginBase` 旧路径导入，配置走 `get_config()` / `update_config()`。
- 仓库里只有本插件。早期那套 **蓝光电视剧分集（BlurayEpisodes）** 已移除，它的 `system_version` 锁死 `>=3.1.5,<3.1.7`，在 3.1.8 上本来就装不上。

## 已知限制

- 只处理形如 `含 BDMV 子目录的盘目录` 的本地路径。ISO 镜像不处理。
- 集序依赖 STREAM 里 m2ts 的 mtime 能反映制作顺序。如果原盘的 mtime 被重写（例如拷贝时未保留时间戳、或打包工具统一改了时间），集序会退回按 clip 名排序，可能与真实集序不符——此时只能用 `episode_offset` 平移，无法修正乱序。
- 一集由多个 playitem 拼接的盘不会被识别：规则 1 只认单 playitem 的播放列表。这类盘的正片会被当作"多片段 = 菜单/play-all"排除，需要放宽规则 1，但放宽后盘内的假 play-all（实测每盘都有一对 100+ playitem、时长落在正片区间的诱饵）会混进正片，两者目前没有可靠判据区分。
- 只做集数、集序和文件拆分，**不改字幕、不重编码**。每集目录内是该集自己的 BDMV 结构。
- 同一季的多张盘必须放在同一个上级目录下（`DISC1`/`DISC2`/`DISC3` 并列），否则无法跨盘连续编号。

## 验证方法

拿一季已知集数的原盘跑预览。参考真值（`Preacher.S01` 三张盘）：

| 盘 | 集数 | clip | 花絮 |
|---|---|---|---|
| DISC1 | 4 | 00334 / 00335 / 00336 / 00337 | 2 |
| DISC2 | 4 | 00335 / 00336 / 00337 / 00348（playlist 00362） | 0 |
| DISC3 | 2 | 00334 / 00335 | 3 |

集号应为 1-4 / 5-8 / 9-10，共 10 集。

## 开发

```bash
python tests/test_mpls.py      # MPLS 解析：24 项
python tests/test_planner.py   # 分集判定：120 项
```

两个模块均不依赖 MoviePilot 运行时，可离线单独测试。
