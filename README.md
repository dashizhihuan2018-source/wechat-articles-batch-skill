# 批量整理微信公众号文章skill

为 Codex 等支持 `SKILL.md` 的代理提供公众号文章批量采集路线选择、去重归档、分类/摘要工作流和验收要求。附标准库本地整理器与合成测试，不捆绑第三方采集程序，不提供绕过登录/风控的能力。

## 使用

将本仓库克隆到本地技能目录：

```bash
git clone https://github.com/dashizhihuan2018-source/wechat-articles-batch-skill.git "${CODEX_HOME:-$HOME/.codex}/skills/wechat-articles-batch"
```

若目标目录已存在，先检查其中修改，勿覆盖。重新打开会话让客户端发现技能（实际发现仍以客户端为准），输入：

> 使用 $wechat-articles-batch，把我提供的这些公众号文章按日期、主题整理为 Markdown 和 CSV 索引，列出缺失与失败项。

中文显示名称：**批量整理微信公众号文章skill**；机器标识：`wechat-articles-batch`。

- [技能入口](SKILL.md)
- [热门项目调研与来源](references/research-2026-10-27.md)
- [技术路线](references/routes.md)
- [Obsidian同步助手路线与真实本地样本核验](references/obsync-route.md)
- [输入输出约定](references/data-contract.md)

## 本地整理器

先将合法取得的文章转换为[规范化JSONL](references/data-contract.md)。然后：

```bash
python3 scripts/organize_exports.py --input /path/to/normalized.jsonl --output /path/to/new-batch
python3 -m unittest discover -s tests -v
```

Python 3.10+，无第三方运行依赖。它生成 Markdown、JSONL/CSV 索引与覆盖报告；不联网、不读取微信、不采集登录态、不下载图片、不自动生成AI摘要。输出重复运行可校验一致性，不覆盖不同内容。

## 当前能力边界

2026-10-27已通过Google原生浏览器检索及GitHub API核验候选。调研发现：高星项目可能已停维；微信读书最新文章接口不等于历史列表。项目对比记录星数与核验日期，不声称全网排名。公众号官方文档本次未核实；14项合成离线测试通过，不代表微信在线采集通过。

正文版权归原作者；技能代码许可证不覆盖采集内容或第三方程序。默认只在本地保存授权资料，公开仓库不应包含文章正文、用户输入清单、凭据或个人笔记。

补充核验：Obsync候选路线已有本机插件REA分析、4项合成回放，以及1篇真实公众号本地导出检查（13张本地图片均可读、正文整理保真）。另发现同名文章目录会混有其他平台内容，须按source_url筛选。上述不代表当前在线获取过程、原文完整性对照或整号100篇采集通过；真实样本及图片未公开。

原创技能和脚本按 [MIT](LICENSE) 发布。© 2026 Nankong。
