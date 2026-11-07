# 本地开发用假数据

这些 JSON 是 `sysinfo.collect()` 的输出快照，用来**在没有 OES 设备的情况下预览界面**。
`sysinfo.py` 读到 `OESBOOT_FAKE` 环境变量时会直接加载指定文件，不执行任何真实探测命令。

```bash
# 默认：正常态
make preview

# 指定场景
OESBOOT_FAKE=$PWD/dev/fake-fail.json OESBOOT_PORT=8765 python3 app/server.py
```

然后打开 <http://127.0.0.1:8765>。

## 四个场景

| 文件 | 模拟的状态 | 界面应显示 |
| --- | --- | --- |
| `fake-status.json` | 一切正常，可以升级 | 绿卡「可以升级」 |
| `fake-hold.json` | 升级窗口已开、还没开始装包 | 「已就绪 · 等待升级」（**不是**让路中） |
| `fake-fail.json` | 上次升级失败、现场已归档 | 红/黄卡「上次升级没有生效」+ 导出按钮 + 卡住的包名 |
| `fake-layout.json` | `/boot` 与 u-boot 读取的分区不同 | 黄卡「引导布局不规范，建议修正」，仍可点「启动升级守护」 |

## 字段契约

界面读取的是 `upgrade` 对象内的字段，别放错层级：

| 路径 | 用途 |
| --- | --- |
| `upgrade.verdict` / `headline` / `summary` | 结论卡 |
| `upgrade.checks[]` | 检查项列表，按 `key` 判定（`dpkgcfg`、`layout` 等） |
| `upgrade.watch` / `upgrade.window` | 升级监视与窗口状态 |
| `upgrade.result` | 上一轮升级结算（`state=success/failed/idle`、`fail_pkg` 等） |
| `upgrade.report` | 现场报告文件元数据（`exists`、`saved_path`） |
| `chain` / `disks` / `guard` / `issues` | 概览、诊断页 |

这些字段的权威定义是 `app/core/sysinfo.py` 的 `collect()` 与 `upgrade_status()`。判定逻辑改动后，请同步更新这里的快照，并在 `tests/test_core.py` 里补单元测试。

## 加新场景

```bash
# 在设备上抓一份真实状态（如果方便）
curl --unix-socket /var/apps/oesboot/target/app.sock \
     http://localhost/api/status > dev/fake-mycase.json
```

- 文件用 `dev/local-*.json` 命名可避免误提交（已在 `.gitignore` 中）；
- 提交前**必须脱敏**：主机名、序列号、内网 IP、MAC、PARTUUID 建议替换成示例值；
- 在 `CONTRIBUTING.md` 的预览说明里补一句新场景的用途。

> 假数据只用于界面渲染，**不参与任何测试判定**。测试用 `tests/test_core.py` 里的 `make_info()` 构造状态。
