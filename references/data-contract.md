# 本地规范化输入与输出

`scripts/organize_exports.py` 只依赖Python 3.10+标准库，读取UTF-8 JSONL（每行一篇），不接受原生第三方导出/HTML。先由代理依据实际导出结构映射，再运行。一个最小的**合成示例**：

```json
{"url":"https://mp.weixin.qq.com/s/synthetic-example","title":"合成示例","account":"测试号","author":"","published_at":"2026-10-27","content_md":"这是合成正文，不是真实文章。","status":"ok","tags":["测试"],"fetched_at":"2026-10-27T08:00:00+08:00","collector":"local-export"}
```

输入字段：`url`必填；`title/account/author/published_at/content_md/status/tags/fetched_at/collector`可选。未知字段拒绝，避免把整个原始API对象/凭据意外透传。`published_at`用YYYY-MM-DD或含时区ISO时间（按Asia/Shanghai计算筛选日期）；`fetched_at`为空或含时区ISO时间。`status`为ok、missing_body、blocked、deleted、error。ok但没有正文会转missing_body；不是ok的内容不当作正文导出。`tags`仅保留用户指定或有依据分类，脚本不自动分类。

URL只接受`https://mp.weixin.qq.com/s/<公开短链>`或`http(s)://mp.weixin.qq.com/s?__biz=...&mid=...&idx=...`，HTTP统一HTTPS；兼容`appmsgid/itemidx`别名；长链允许缺idx并按1处理。规范化保留公开文章定位所需biz/mid/idx/sn，去掉其他查询参数/fragment。含身份凭据的原始URL不得出现在报告；规范化不是完整隐私检测，正文/标题仍须人工检查。没有URL的本地文档不要伪造链接，单列人工整理，不传入本脚本。

身份：长链`biz+mid+idx`，短链公开路径。sn不参与身份但可保留在输出链接中；短/长链不自动互认。多图文idx保留。重复同身份优先保留ok且非空正文，其次最新fetched_at，再其次后出现的记录；选中条目为一次快照，**不保存所有历史修订**。不同身份的相同正文仅以`same_content_as`标记，不丢弃来源。需要修订史时保留私有原始导出和独立批次。

输出：

- `articles/*.md`：只为ok正文生成，JSON引号兼容YAML frontmatter，哈希文件名避免重名/路径穿越；正文按输入保留，不执行内容。媒体默认仍为原有链接。
- `catalog.jsonl`：所有筛选后的唯一文章的元数据/状态/正文哈希/相似来源/归档相对路径，不包含正文。
- `catalog.csv`：便于表格查看，防公式注入；UTF-8 BOM。
- `INDEX.md`：本地导航。
- `report.json`：输入/筛选/重复/最终各状态计数，以及去重前`input_statuses`（保留重复条目的失败尝试计数），附明确的非全历史覆盖说明。

目录不含原始请求、Cookie和AI摘要。新输出目录写入；重复运行若完整文件集与内容相同则no-op，任意差异拒绝覆盖，改用新的批次目录。异常退出时产物可能不完整，检查后改用新目录；不自动删除已有文件。无可靠日期的条目在启用日期过滤时剔除并计数，不默认当今天。本脚本不复制本地附件，迁移Markdown后原有相对图片路径可能失效；需要离线包时由适配器复制授权附件并按最终文章位置重写链接后另行验收，不能仅见Markdown文件就称离线包完成。

验证：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/organize_exports.py --help
```

测试均为合成输入，无真实公众号文章、个人账号、登录态或网络请求。未测试微信登录/在线采集/离线图片。摘要及分类由执行代理另做，并标记模型处理范围、来源与待核项。
