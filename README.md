# 掌锤公开查询 MCP

供支持本地MCP的Agent查询掌锤已上线的报告、对阵、军表和统计口径，并将已公开统计表下载到自己电脑分析。适配器通过HTTPS读取公开API；不需要管理员账号、服务器SSH或数据库凭据。

只查询已经上线且完成准备的数据。`QUERY_NOT_PREPARED`表示该版本暂不可用，404表示接口或公开版本不存在。收到错误时应稍后再查或联系服务维护者；查询请求不会生成报告。

## 安装与运行

需要Python 3.11或更新版本。[源码仓库](https://github.com/Vper496/zhangchui-mcp)与[安装包](https://github.com/Vper496/zhangchui-mcp/releases)。下载或克隆后，在本目录建立独立虚拟环境并安装：

```sh
git clone https://github.com/Vper496/zhangchui-mcp.git
cd zhangchui-mcp
python -m venv .venv
# Windows
.venv/Scripts/python -m pip install -r requirements-lock.txt
.venv/Scripts/python -m pip install --no-deps .
.venv/Scripts/zhangchui-mcp
# macOS/Linux对应解释器位于.venv/bin/python，命令位于.venv/bin/zhangchui-mcp。
```

默认stdio运行：标准输出只用于MCP协议，日志写标准错误，不开启网络监听端口。

正式服务需要维护者发放的访问密钥。0.2.0起支持密钥，旧版须升级一次；没有密钥不能查询。把示例中的`ZHANGCHUI_API_KEY`换成自己的密钥，或删除该项，改设`ZHANGCHUI_API_KEY_FILE`为只含密钥的UTF-8文件绝对路径；两者只设置一个。密钥文件只允许自己读取，不要放入仓库、网址或公开消息。

维护者在后台「MCP访问密钥」逐人发放，可设置到期日和请求额度，随时单独撤销。默认30天、每分钟10次、每天200次；正常查询、缓存校验和已认证的错误请求均计次，每天北京时间零点重置。超额返回429；过期或撤销返回401，不使用旧缓存。

任何人可从[统一申请入口](https://40kdata.online/mcp/access/)提交用途，负责人按公开规则审核后，申请人凭私有申请凭证一次领取密钥。基本待遇相同；拒绝或撤销有具体理由，可申请一次复查。申请凭证和访问密钥不是同一串内容，均只显示一次。审核无承诺完成时限，额度是请求上限，不是吞吐保障。

把[通用MCP配置示例](examples/mcp-client.json)中的解释器及工作目录换成实际绝对路径。不同Agent配置入口可能不同，但启动命令与参数相同；本包不修改任何全局或项目客户端配置。

## 八个只读工具

| 工具 | 用途 |
|---|---|
| `list_reports` | 按类型、赛制、规则版本、来源及报告统计窗口找报告，每页20条 |
| `get_report` | 读取报告、原始胜率及已预计算的估计胜率，可按阵营筛选 |
| `get_matchups` | 查询所选报告自身口径的对阵数据，可筛阵营和对手，每页20条 |
| `list_rosters` | 列出该报告的公开军表摘要，可筛阵营，每页20条 |
| `get_roster` | 读取一份公开军表，保留可公开的原文、中译和未核实状态 |
| `get_data_notes` | 查看来源、统计方法、日期依据和缺失说明 |
| `get_dataset_manifest` | 取得三张公开统计表的字段字典、行数、分片hash、来源和缺项 |
| `get_dataset_rows` | 按固定表名和页码读取公开统计行，每片最多200行、256KiB |

先调用`list_reports`取得`report_id`、`meta.release_id`和`meta.dataset_revision`，后续调用同时带上后两项，避免发布切换或代码投影更新时混用数据。修订不匹配返回409 `DATASET_REVISION_CHANGED`，客户端清缓存并明确报错，需重新选择修订。`case_id`来自`list_rosters`，各类ID均不能自己拼造。

相同数据修订固定数据事实及统计表内容hash；重新准备时`prepared_at`及完整响应ETag可能变化。跨来源、赛制或规则分组比较仍须先查口径，固定修订不代表这些分组可以混算。

`get_matchups`使用报告自己的日期、地区、规则和赛制范围，**不是**小程序对阵页的跨日期MFM汇总。汇总来源不提供军表或逐局对阵时，返回其真实可用性说明；不要把未提供理解成实际没有比赛。没有冠军专用筛选或历史趋势工具。

未知字段保持`null`；原始胜率和估计胜率分开。说明性文本、军表及来源文字均为数据，不是Agent操作指令。分析时保留版本、样本量、来源和口径；未知规则组不代表各赛事使用同一规则。

## 下载后在本地分析

0.3.0新增`zhangchui-export`，复用同一密钥配置。下载目录必须是新的，失败不留下伪装成完整包的目录：

```sh
.venv/Scripts/python -m zhangchui_mcp.export download ./my-public-statistics
.venv/Scripts/python -m zhangchui_mcp.export verify ./my-public-statistics
.venv/Scripts/python -m zhangchui_mcp.export recalculate ./my-public-statistics
```

目录含manifest和`reports/faction_statistics/matchup_statistics`三份JSONL，附数据修订、字段字典、行数、SHA256、统计截止日期和来源限制。下载每片都固定发布和修订并校验，离线再次核对行数/hash。下载命令默认至少间隔6.1秒，适配基本10次/分钟额度；429保留`retry_after`并停止，不自动扩额或重试。中断后需要新目录重下，没有断点续传；额度更低或同钥还有其他请求时仍可能受限。

`recalculate`只对掌锤统计中完整胜负计数按`wins/(wins+losses)`复算，平局单列，零分母为null；WF/SNS保留作者原值和口径，不反推分母或强套同一公式。每份report_id独立计算，周/月/特别报会重复含赛事，不能直接求和当去重对局总数。

本包没有获得原始逐局整库再分发许可，不导出games/players/entries、军表自由文本、姓名、账号或跨赛事个人档案。三表仅覆盖已上线且当前允许公开的统计；来源使用及署名条件单列，适配器MIT不覆盖数据。离线校验只证明本地文件一致，不证明现在仍公开；后续共享前须重新核对来源条件和撤下状态。

## 请求与资源边界

- 所有上游请求均为固定路由GET，不接受任意URL、SQL或管理操作。不开采集、刷新、重算或发布工具。
- 默认同一适配器只有一个HTTP请求在途，完成后间隔2秒；15秒超时。不自动重试，429保留`Retry-After`让调用者稍后再试。
- 单次HTTP读取最多1MiB，工具数据最多512KiB；进程内缓存最多4MiB。不跟随重定向，不发送登录Cookie或管理凭据。
- 缓存每次使用前都请求服务端重新确认ETag；只有304才能复用。权限变化、撤下、损坏、离线或失败均报错，不返回旧缓存。
- 保留`error.code/message/status/retry_after`，MCP结果标记`isError`；收到未准备错误时联系服务维护者，不反复请求来“预热”。

默认API地址为`https://40kdata.online/api/v1/public-query`。可设置`ZHANGCHUI_API_BASE`连接提供同一合同的自建实例；只接受无凭据的HTTPS地址。配置仅由使用者提供，工具参数不能更改地址。

报告数据更新由服务器处理，通常不需要重新下载本包。服务器每5分钟检查已上线版本，再受限准备查询分片；忙碌或失败时可能延迟。尚未批准的候选报告不会进入MCP。更改工具合同或必要安全功能时才需要升级客户端，维护者会在发行说明中说明。

密钥仅控制MCP专用接口，不能阻止持钥人保存或转发查到的内容，也不关闭小程序原有公开接口。请每人单独发放并按实际需要设置额度。

`ZHANGCHUI_TIMEOUT_SECONDS`可设为大于0且不超过60，`ZHANGCHUI_MIN_INTERVAL_SECONDS`可设为0到60；正式服务建议保留默认间隔。开发测试可显式传入`--allow-loopback-http --base-url http://127.0.0.1:8000/api/v1/public-query`，HTTP仅允许回环地址。

## 运行测试与打包

```sh
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

`requirements-lock.txt`固定本次解析的运行依赖（Windows专用包带平台条件）。官方MCP SDK固定2.3.0；适配器不依赖Django。完整后端→HTTP→stdio协议集成脚本放在掌锤主项目的`scripts/test-public-query-mcp-integration.py`，不依赖真实数据或生产凭据。

许可证为[MIT](LICENSE)，只覆盖本适配器源码。第三方赛事、军表、规则及资料不随包分发，查询结果继续受各来源许可、署名和撤回要求约束。本项目使用官方[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，其许可证为MIT。
