# 贡献指南

感谢你愿意改进这个项目。它跑在别人的 NAS 上，且会写引导分区，所以这里的规矩比一般 Web 项目严一些，请耐心看完。

## 开发环境

**不需要 OES 设备**。核心逻辑与界面都可以在普通 Linux / macOS 上开发与测试。

```bash
git clone https://github.com/ranlove99/oes-boot-mgr.git
# 或先 Fork 到自己的账号，再 clone 你的 fork
cd oes-boot-mgr
python3 -m pip install pytest        # 唯一的开发依赖
make verify                          # 语法 + 一致性 + 单元测试
make preview                         # 用假数据起本地界面：http://127.0.0.1:8765
```

`make preview` 通过 `OESBOOT_FAKE` 指向 `dev/fake-*.json`，模拟不同设备状态（正常、让路、布局不符、上次升级失败）。切换 fixture 即可预览对应界面，说明见 [dev/README.md](dev/README.md)。

运行时依赖只有 **Python 标准库**和常见系统命令（`lsblk`、`blkid`、`findmnt`、`flock`、`rsync`、`journalctl`）。fnOS 自带 python312；请勿引入第三方 Python 包，否则用户装不上。

## 目录约定

| 路径 | 作用 | 是否打进 fpk |
| --- | --- | --- |
| `manifest`、`ICON*.PNG` | fnOS 应用元数据 | 是 |
| `cmd/`、`config/`、`wizard/` | 应用生命周期脚本、权限与安装向导 | 是 |
| `app/` | 服务端、界面、核心逻辑与守护脚本 | 是（打成 `app.tgz`） |
| `docs/`、`dev/`、`tests/`、`tools/`、`.github/` | 文档、假数据、测试、构建工具 | 否 |

`fnpack` 依据 fnOS 约定打包，**不要**把 `app/` 改成 `src/`，也不要给 `cmd/` 里的脚本加 `.sh` 后缀——应用中心按文件名调用它们。

## 提交前必做

```bash
make verify     # = lint + check + test
```

`make check` 会强制校验下面这些，任何一项不过 CI 都会红：

1. `manifest` 的 `version`、`app/assets/oes-boot-sync.sh` 的 `VERSION`、界面标题、`README.md` 顶部版本徽章四处一致；
2. fpk 入口文件齐全、关键脚本有可执行位；
3. 所有文本为 **LF**（`.gitattributes` 已声明，请勿用 Windows 编辑器改写）；
4. 代码中不存在「引号内含 `>` `|` `&`」的 dpkg 钩子写法；
5. 不含密钥、令牌或开发机绝对路径。

第 4 条来自一次真实事故：1.1.4 写入的 `post-invoke "…sync >/dev/null 2>&1 || true";` 让 dpkg 在**每次操作前**解析配置失败，整台机器的包管理瘫痪，OTA 装内核 4 秒就报 `failed to install`，排查被带偏了五轮。现在的钩子只调用一个永远 `exit 0` 的 wrapper 脚本，**不要再往引号里塞任何 shell 元字符**。

## 安全红线

这些不是风格建议，是硬约束：

- **只在真正需要时挂载引导分区**；升级安装阶段（`boot_changing` 或 `ota_busy`）不得挂载、不得写 `fnEnv.txt`。
- 写入引导分区前必须校验内核文件完整（大小、格式、复制期间源文件未变）；宁可跳过一轮，也不能把写了一半的内核同步过去。
- 任何改 `/etc/fstab` 的操作都要先备份、`mount -a --fake` 校验语法、失败自动还原，并校验 `fnEnv` 指向的文件真实存在。
- 脚本返回 0 **不等于**同步成功。让路、跳过、拒绝同步也都是 0，必须输出 `RESULT=done` / `RESULT=skip:<原因>` 供上层判定，不得让界面出现「假成功」。
- 手动触发（用户点的按钮）不受守护总开关 `ENABLED` 影响；只拦自动触发。
- 不得触碰数据分区、不得联网、不得回传诊断报告。

## 加功能时

- 涉及判定逻辑（能不能升级、同步算不算成功）的改动，**必须补单元测试**到 `tests/`，用 `make_info()` 构造状态，不要依赖真实磁盘。
- 涉及取证能力的改动，想清楚「用户把报告发过来时，能不能只看报告就定位问题」。现场报告已有 17 段，新信息请归入合适段落而不是另起一个散文件。
- 界面文案要与判定一致。历史上出现过界面写「可以升级」但布局检查根本没接进判定的情况，这类不一致按 bug 处理。

## 发布流程

1. 更新四处版本号（manifest / 守护脚本 / 界面标题 / README 徽章）；
2. 在 `manifest` 的 `changelog` **最前面**追加新版本说明（单行，`\n` 分隔各版本），同步重新生成 `CHANGELOG.md`；
3. `make verify` 通过；
4. `make build`（会下载 `fnpack`，产物在 `dist/`，不入库）；
5. 在真实 OES 设备上安装并至少验证一次：首页结论、手动同步、`dpkg --audit` 干净、升级流程；
6. 打 tag `vX.Y.Z`，把 `dist/*.fpk` 与 `.sha256` 传到 [GitHub Release](https://github.com/ranlove99/oes-boot-mgr/releases)。

> `manifest` 的 `maintainer` / `maintainer_url`、`LICENSE` 版权人、README 署名须保持一致，`make check` 会校验；改作者时请三处一起改。

## 提交规范

- 提交信息用中文或英文均可，第一行写清「改了什么、为什么」，例如 `修复守护关闭时手动同步被吞掉的问题`。
- 一个 PR 只做一件事。修 bug 请附复现方式与验证结果。
- 不要提交 `dist/`、`__pycache__`、设备上的日志和诊断报告（可能含主机名与内网信息）。

## 报告问题

请使用 [问题反馈模板](.github/ISSUE_TEMPLATE/bug_report.md)，附上 `oesboot diagnose` 输出。没有诊断报告的问题通常无法定位。
