# VP Helper

Ubuntu 上的 XCPC 虚拟参赛终端助手，支持 QOJ、Codeforces ICPC 比赛和 Codeforces Gym。上方显示成绩与完整提交记录，下方保留比赛目录中的普通 shell。

当前版本：0.1.8。

## 安装

支持 Ubuntu 22.04 / 24.04，随仓库提供 Linux x86_64 的独立 Python 3.12 运行时与依赖 wheels。安装器使用隔离环境，保留系统 Python。

```bash
git clone https://github.com/thedyingkai/vp-helper.git
cd vp-helper
bash install.sh
```

安装后可在任意目录使用 `vp` 和 `submit`。Python 运行时与依赖可从仓库离线安装；系统未安装 tmux 时，安装器通过 apt 安装 tmux。

## 使用

将 `ID` 替换为对应平台的数字比赛编号：

```text
vp cf ID
vp qoj ID
vp gym ID
```

例如：

```bash
vp qoj 3169
submit A.cpp
vp history
```

也支持 `vp CONTEST_URL`。计分范围限定为 ICPC；普通 Codeforces Round 的 CF / IOI 计分会在开赛前被拒绝。

首次使用在终端配置账号及已有登录 Cookie，Codeforces / Gym 还需要 API key 与 API secret。配置保存在 `~/.config/vp/config.json`，权限为 600；也可以参考 `config.example.json` 填写。`cf` 与 `gym` 默认共用 Codeforces 配置。

每场比赛在桌面共用一个目录，例如 `2024 ICPC Nanjing Reg`。再次进入保留已有代码和输入数据，更新提交配置与题面；各次 VP 的开始时间和成绩状态独立保存。`submit A.cpp` 在对应比赛目录内运行。

## 成绩与界面

- `vp` 准备题面与提交配置后，在现有账号上启动并验证原站 VP，以平台开始时间计时。
- 按正式参赛资格计算排名，排除打星队与其他 VP；解题数和罚时相同共享名次，最后 AC 用时只决定展示顺序。
- 奖项采用原赛明确配置。资格、规则或榜单完整性无法验证时，保留未知结果。
- `rank` 列保持在成绩表左侧，排名和奖项在同一格内分两行显示，例如上行 `=47`、下行 `Silver Prize`。
- 内置 submit 在服务器确认新提交编号后立即通知面板显示 `PENDING`，随后用平台判定替换；本人提交与榜单查询独立执行。
- 提交记录按时间从新到旧完整展开，`practice` 标注紧贴判定。5 小时后的补题不计入比赛成绩，未通过显示橙色，通过显示紫色。
- 空白、标题与提交列表继承终端默认背景，支持终端自身的透明效果。
- 最终成绩保存到桌面的 `vp_history.csv`。

在上方面板用方向键、`PgUp` / `PgDn`、`Home` / `End` 或鼠标滚轮查看提交。`Ctrl+B`、`D` 脱离 tmux 后继续同步；再次运行同一场比赛命令可恢复。按 `q` 关闭评分面板。

QOJ 已完成真实账号联调，包括南京与香港的正式排名、奖项和提交记录。CF / Gym 的账号流程需要已有登录会话和 API 凭据；请按实际平台状态核实。

## 赛时排名与统计

首次运行比赛命令时，程序根据平台 ID 读取比赛信息，自动获取该场原赛资格、榜单和提交记录。有完整原赛提交记录时，先用它重算终榜，与平台每支队伍的每题结果和罚时核对，再按 VP 当前时间每秒回放原赛成绩，计算当前排名与统计。QOJ 使用 XCPCIO 原赛记录；CF 原生参赛队伍使用官方 API 提交记录，导入 ghost 仍须匹配原赛资格和记录。

有待判提交时，在上一轮查询返回后约 2 秒再次查询本人判定；其余时间约 5 秒，网络故障时退避重试。平台榜单约 30 秒重新核对；这些间隔不包含网络耗时。CF 请求统一遵守 API 限速。

- `submitted`：首次通过前的有效计分提交次数，含待判。
- `attempted`：有有效尝试的正式队伍数。
- `accepted`：通过的正式队伍数。

统计包含本人赛内提交，排除打星和赛后补题；编译错误按本场计分规则处理。封榜时公开统计隐藏冻结后的结果，rank 显示 `?`，当前 VP 的原站解封后恢复。缺少资格数据或完整事件记录时，面板显示具体原因；只有终榜时，赛时排名与统计保持 `—`。奖项只使用明确的原赛奖牌配置。

## 数据与第三方组件

仓库和安装包不包含任何比赛数据。首次使用时自动获取，可识别的 ICPC / CCPC 区域赛从 [XCPCIO board-data](https://github.com/xcpcio/board-data) 读取正式参赛资格、奖牌配置和原赛提交记录，按学校、队名和成员身份与平台榜单匹配。资格校验通过、提交记录复算与终榜一致后，才保存相应缓存。

生成的数据保存在用户自己的 `~/.cache/vp/originals/`（设置 `XDG_CACHE_HOME` 时使用 `$XDG_CACHE_HOME/vp/originals/`），后续使用复用缓存。缓存可以删除，下次使用会重新获取；首次获取需要网络。不需要修改仓库或手动添加比赛 JSON 文件。无法取得完整可靠原赛数据的比赛，仍显示本人成绩和提交，并说明参考排名或统计缺失的原因。XCPCIO 的 MIT 许可证保留在 `third_party/xcpcio/LICENSE`。

提交器来自 [hikariyo 的 QOJ submit.py](https://gist.github.com/hikariyo/b3cec2e736d87be57d1e4faafb04f908) 与 [ehnryx/cf_submit](https://github.com/ehnryx/cf_submit)，固定版本、兼容补丁与现有许可证保存在 `third_party/`。Python 运行时的来源与校验值见 `runtimes/manifest.json`。

本仓库仅包含运行程序、安装依赖和使用文档；比赛数据、测试代码、测试样例、账号配置及个人比赛文件不纳入仓库。
