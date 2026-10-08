# VP Helper

Ubuntu 上的 XCPC 虚拟参赛终端助手，支持 QOJ、Codeforces ICPC 比赛和 Codeforces Gym。上方显示成绩与完整提交记录，下方保留比赛目录中的普通 shell。

当前版本：0.1.7。

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
- 提交记录按时间从新到旧完整展开。5 小时后的补题不计入比赛成绩，未通过显示橙色，通过显示紫色。
- 空白、标题与提交列表继承终端默认背景，支持终端自身的透明效果。
- 最终成绩保存到桌面的 `vp_history.csv`。

在上方面板用方向键、`PgUp` / `PgDn`、`Home` / `End` 或鼠标滚轮查看提交。`Ctrl+B`、`D` 脱离 tmux 后继续同步；再次运行同一场比赛命令可恢复。按 `q` 关闭评分面板。

QOJ 已完成真实账号联调，包括南京与香港的正式排名、奖项和提交记录。CF / Gym 的账号流程需要已有登录会话和 API 凭据；请按实际平台状态核实。

## 数据与第三方组件

原赛正式资格与奖牌配置使用 [XCPCIO board-data](https://github.com/xcpcio/board-data)，南京与香港的固定版本资格数据包含在 `vp_helper/data/`；其 MIT 许可证随数据保留。其他可识别的比赛按学校、队名和成员身份匹配原赛存档。

提交器来自 [hikariyo 的 QOJ submit.py](https://gist.github.com/hikariyo/b3cec2e736d87be57d1e4faafb04f908) 与 [ehnryx/cf_submit](https://github.com/ehnryx/cf_submit)，固定版本、兼容补丁与现有许可证保存在 `third_party/`。Python 运行时的来源与校验值见 `runtimes/manifest.json`。

本仓库仅包含运行程序、安装依赖和使用文档；测试代码、测试样例、账号配置及个人比赛文件不纳入仓库。
