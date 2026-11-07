# 排障手册

本文件把项目历史上真实踩过的坑按「症状 → 证据 → 原因 → 处理」整理，绝大多数问题看一眼现场报告就能定位。

## 先看这三行

```sh
oesboot diagnose                       # 完整诊断报告
grep ENABLED /etc/oesboot.conf         # 守护总开关
dpkg --audit                           # 包管理器是否健康（无输出 = 干净）
```

涉及升级过程的，再抓一份「升级现场」（守护页 → 抓取并导出升级现场）。**抓取时会重新采集**，拿到的就是当前状态，不是历史缓存。

## 症状索引

| 症状 | 直接跳到 |
| --- | --- |
| 升级 4 秒就报 `failed to install`，`/boot` 里没有新内核 | [1](#1-四秒失败) |
| 版本升了，但 `uname -r` 还是旧内核 | [2](#2-升级成功但没生效) |
| 点「立即同步」显示「同步完成」却没有变化 | [3](#3-假成功) |
| 没点升级，界面却一直「让路中」、次数一直涨 | [4](#4-一直让路) |
| 升级完显示「无结算记录」 | [5](#5-无结算记录) |
| 界面长期显示「可以升级」，实际布局不对 | [6](#6-误报可以升级) |
| 导出的现场报告内容是旧的 | [7](#7-导出旧现场) |
| 点「启动升级守护」提示「还有未通过项」 | [8](#8-启动被挡住) |
| 首页提示「启动分区内核与系统不一致」 | [2](#2-升级成功但没生效) |

---

## 1. 四秒失败

**现象**

```
07:20:40 trim-update[1604770]: updating 'linux-image-6.18.18.c1191-trim'...
07:20:44 trim-update[1604770]: failed to install 'linux-image-6.18.18.c1191-trim'.
```

从 `updating` 到 `failed` 只有 4 秒，`/boot` 和 eMMC 都找不到 `c1191`，且每次都是同样 4 秒、同一个包。

**证据**

```sh
dpkg --audit
# dpkg: error: configuration error: /etc/dpkg/dpkg.cfg.d/99oesboot-sync:1: unbalanced quotes in 'post-invoke'
```

**原因**：1.1.13 及更早版本写入的 dpkg 钩子是

```
post-invoke "/usr/local/sbin/oes-boot-sync sync >/dev/null 2>&1 || true";
```

dpkg 的配置解析器**不按 shell 规则处理引号**。只要引号里出现 `>`、`|`、`&` 中任意一个，就在每次 dpkg 操作前解析失败并直接退出。于是内核包连解包都没轮到，`dpkg --audit` 也报同样的错。这与引导布局、守护让路、SSD 都无关。

**处理**

```sh
sudo rm -f /etc/dpkg/dpkg.cfg.d/99oesboot-sync
sudo dpkg --audit            # 无输出即恢复
```

然后装 1.1.14 及以上。新版钩子改为无引号形式 `post-invoke /usr/local/sbin/oesboot-dpkg-hook`，逻辑收进永远 `exit 0` 的 wrapper；应用启动时还会静默自愈一次。**这一步不做，装什么都会失败。**

> 现场报告第十六段会直接抄出 `term.log` / `dpkg.log` 原文。如果 `dpkg --audit` 干净但仍然 4 秒失败，看那一段的 `dependency problems` 或 `/boot` 权限错误。

---

## 2. 升级成功但没生效

**现象**：系统版本变了（如 `1.2.0701 → 1.2.1000`），`/boot` 里有新内核，但 eMMC 引导分区仍是旧的，重启后 `uname -r` 不变。

**原因与处理**，按出现概率排列：

| 原因 | 怎么确认 | 处理 |
| --- | --- | --- |
| 守护总开关关着 | `grep ENABLED /etc/oesboot.conf` → `ENABLED=0` | 见下 |
| 20 秒监视只拉起 timer、没开开关 | 报告第十七段 `累计自动同步 0 次` | 装 1.1.18+，或手工置 1 |
| 内核模块检查误判（1.1.15） | 守护日志有 `内核只装了一半，拒绝同步` | 装 1.1.16+，两个模块目录都查 |
| 「启动升级守护」被布局项挡住 | 弹「还有未通过项」 | 装 1.1.15+，或见 [8](#8-启动被挡住) |

打开开关并手动同步：

```sh
sudo sed -i 's/^ENABLED=.*/ENABLED=1/' /etc/oesboot.conf
grep ENABLED /etc/oesboot.conf          # 若文件里没这行，用下一行补
echo 'ENABLED=1' | sudo tee -a /etc/oesboot.conf

sudo /usr/local/sbin/oes-boot-sync sync --reason manual
```

看最后一行输出：

- `OK: 引导分区已同步（rootdev=…, kernel=…, 触发=manual）` → 成功，重启后 `uname -r` 就是新内核；
- `RESULT=skip:<原因>` → 被某道校验拦下，按原因处理；
- `WARN: … 内核只装了一半` → 装 1.1.16+ 再试。

**验证**

```sh
findmnt -no SOURCE /boot
ls /lib/modules/*/ /usr/lib/modules/*/ 2>/dev/null | head
systemctl is-active oesboot-sync.service oesboot-check.timer
```

> 升级完成后先别急着改引导布局。先把内核同步过去、重启验证 `uname -r`，稳定几天再考虑 hybrid。

---

## 3. 假成功

**现象**：弹窗日志

```
开始同步引导分区……
oesboot guard disabled
同步完成
```

0 秒完成 60MB 复制是不可能的——它一个字节都没写。

**原因**：1.1.16 及更早版本脚本开头有 `ENABLED != 1 就 exit 0`，用户亲手点的手动同步也被吞掉；上层只看退出码，0 就渲染成「同步完成」。同时界面顶部还写「守护运行中」，因为它只看 systemd 单元 active，不看配置。

**处理**：装 1.1.17+。现在开关只管自动触发；脚本结束输出 `RESULT=done` / `RESULT=skip:<原因>`，管家据此判定，没真同步就明确报错。临时办法见 [2](#2-升级成功但没生效) 的两行命令。

---

## 4. 一直让路

**现象**：没点升级，界面却显示「让路中」，「已让路」次数每 20 秒涨一次。

**原因**：1.1.18 及更早版本只要升级窗口开着，20 秒一次的巡检就无条件 `exit 0`。窗口是点「启动升级守护」打开的，用户可能十分钟后才去点升级，这十分钟里一次都没真正比对过，却把空转记成让路。

**处理**：装 1.1.19+。现在让路只由「`/boot` 内核文件 120 秒内变过」或「`dpkg`/`apt` 在跑」触发；界面区分「已就绪 · 等待升级」与「升级中 · 已让路」，并写瞬时标记 `/run/oes-boot-sync/holding`。

升级已完成的，结束窗口：

```sh
sudo rm -f /run/oesboot-upgrade
sudo systemctl stop oesboot-watch.timer
```

---

## 5. 无结算记录

**现象**：升级明明成功了，界面显示 `state=none` / 「无结算记录」。

**原因**：1.1.15 及更早版本结算依赖 `/run` 里的运行记录，而升级完成必然重启，`/run` 是 tmpfs，一重启就没了；旧逻辑把它当「历史残留基线」丢弃。**另一个极端**是反过来，拿残留基线凭空造出一条「上次升级失败」。

**处理**：装 1.1.16+。现在看版本/内核是否真的变了：变了就按「升级后重启」正常结算；只有两者都没变且基线超过 24 小时，才当残留丢弃。

---

## 6. 误报可以升级

**现象**：`/boot` 明明挂在 SSD、u-boot 明明只读 eMMC，界面一直绿卡「可以升级」。

**原因**：1.1.11 及更早版本的检查项里根本没有「布局一致性」这一项。1.1.11 写了布局修正功能，却没接进判定，直到 1.1.12 才补成独立检查项。

**现状**：布局检查是 **warn** 级，会显示但不阻断升级——它影响「重启后生不生效」，不影响「装包成不成」。真正会阻断的是 `dpkgcfg`、`kernel`、`rootdev` 这些 error 项。

---

## 7. 导出旧现场

**现象**：14:49 点「导出升级现场」，拿到的是 14:35 的内容，窗口时间、内核、版本全是过期的。

**原因**：1.1.18 及更早版本只在报告文件**不存在**时才重新抓取，存在就直接给旧文件。

**处理**：装 1.1.19+，一律重新抓取。旧版可先删除再抓：

```sh
sudo rm -f /var/lib/oesboot/ota-report.txt
```

---

## 8. 启动被挡住

**现象**：点「启动升级守护」弹出「还有未通过项，请先点一键修复」，但点「一键修复」之后还是一样。

**原因**：1.1.14 之前布局项被定为 error 级挡住启动，而「一键修复」只做 dpkg 修复、重装守护、同步引导分区三件事，不处理布局，形成死循环。

**处理**：装 1.1.15+。布局降为 warn，启动守护只拦 error 项，提示语会具体列出是哪几项。

---

## 诊断命令速查

```sh
# 启动链
cat /proc/cmdline | tr ' ' '\n' | grep -E 'root|ubootpart'
findmnt -no SOURCE,FSTYPE /boot
blkid -s PARTUUID -o value /dev/mmcblk1p1
cat /boot/fnEnv.txt

# 守护
grep -E 'ENABLED|LOCK|OTA_YIELD|MIN_AGE' /etc/oesboot.conf
systemctl is-active oesboot-sync.service oesboot-check.timer oesboot-watch.timer
systemctl is-enabled oesboot-sync.service oesboot-shutdown-sync.service
tail -50 /var/log/oesboot-guard.log

# 升级现场
tail -60 /var/log/apt/term.log
tail -30 /var/log/dpkg.log
dpkg --audit
dpkg -l | awk '$1!~/^(ii|rc)$/ && $1~/^[a-z]/{print}'

# 空间（eMMC 引导分区放不下新内核是常见硬原因）
df -h /boot
ls -lh /boot/vmlinuz-* /boot/uInitrd-* 2>/dev/null
```

## 回滚

| 操作 | 命令 |
| --- | --- |
| 回滚引导布局 | `sudo /usr/local/sbin/oes-boot-sync hybrid-rollback` |
| 手动还原备份 | 备份在 `/var/lib/oesboot/backup/`（fstab 与两侧 fnEnv） |
| 切回 eMMC 启动 | `oesboot switch emmc`（重启生效） |
| 彻底失联 | 用官方固件做 U 盘启动盘 → 挂载 `/vol00/RemovableDisk` → 注释掉 `fnEnv.txt` 的 `rootdev=` → 重启即从 eMMC 启动 |

**任何改挂载点或分区表的操作前，请备好启动 U 盘并备份数据。**
