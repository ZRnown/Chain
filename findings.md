# 调查发现：风险评分功能

## 初步发现

### 数据模型检查
**文件**: `src/models.py`

已发现的字段：
- `TokenMetrics` 模型包含 `sol_sniffer_score: Optional[float]` (第30行)
- `TokenMetrics` 模型包含 `token_sniffer_score: Optional[float]` (第31行)
- `FilterConfig` 模型包含 `sol_sniffer_score: FilterRange` (第51行)
- `FilterConfig` 模型包含 `token_sniffer_score: FilterRange` (第52行)

**结论**: ✅ 数据模型已完整支持风险评分

### API调用实现检查
**文件**: `src/data_fetcher.py`

已发现的方法：
- `_fetch_risk_scores()` (第619-628行): 并行获取两个评分
- `_fetch_sol_sniffer_score()` (第630-672行): 获取 SolSniffer 评分
- `_fetch_token_sniffer_score()` (第674-721行): 获取 TokenSniffer 评分
- `fetch_all()` 方法在第56-59行调用 `_fetch_risk_scores()`

**API端点**:
- SolSniffer: `https://solsniffer.com/api/v2/tokens/{chain}/{address}`
- TokenSniffer: `https://tokensniffer.com/api/v2/tokens/{chain_id}/{address}`

**环境变量需求**:
- `SOL_SNIFFER_API_KEY`
- `TOKEN_SNIFFER_API_KEY`

**结论**: ✅ API调用已实现，但需要配置API密钥

### 过滤器检查
**文件**: `src/filters.py`

已发现：
- 第20-21行：过滤器检查列表包含 `sol_sniffer_score` 和 `token_sniffer_score`

**结论**: ✅ 过滤逻辑已实现

### 消息显示检查
**文件**: `src/main.py`

已发现：
- 第84-86行：`build_caption()` 函数显示风险评分
- 格式：`🛡️风险评分: SolSniffer {score} | TokenSniffer {score}`

**结论**: ✅ 消息显示已实现

### Bot命令检查
**文件**: `src/bot.py`

已发现：
- 第785-786行：filter_names 字典包含风险评分显示名称
  - `"sol_sniffer_score": "🛡️ SolSniffer评分"`
  - `"token_sniffer_score": "🛡️ TokenSniffer评分"`
- 第793-811行：过滤器菜单循环包含所有过滤器，包括风险评分
- 按钮格式支持显示已设置的值范围

**结论**: ✅ Bot命令已完整支持风险评分过滤器设置

### State管理检查
**文件**: `src/state.py`

已发现：
- 第20-21行：`_filters_to_dict()` 包含风险评分字段序列化
- 第37-38行：`_filters_from_dict()` 包含风险评分字段反序列化

**结论**: ✅ State管理已完整支持风险评分过滤器

### API实现详细检查
**文件**: `src/data_fetcher.py`

#### SolSniffer API (第630-672行)
- **端点**: `https://solsniffer.com/api/v2/tokens/{chain}/{address}`
- **链映射**: solana, bsc, ethereum
- **参数**: `apikey`, `include_tests=true`
- **返回字段**: `data.score` (0-100)
- **环境变量**: `SOL_SNIFFER_API_KEY`

#### TokenSniffer API (第674-721行)
- **端点**: `https://tokensniffer.com/api/v2/tokens/{chain_id}/{address}`
- **链映射**:
  - solana: 1399811149
  - bsc: 56
  - ethereum: 1
- **参数**: `apikey`, `include_metrics=false`, `include_tests=true`
- **返回字段**: `data.tests.score` (0-100)
- **环境变量**: `TOKEN_SNIFFER_API_KEY`

**潜在问题**:
⚠️ 代码中使用了"假设"的返回格式（见第660、710行注释），可能需要验证实际API响应格式

### API文档验证（用户提供）

**SolSniffer API 实际文档**:
- **端点**: `GET /token/{address}` (不是 `/tokens/{chain}/{address}`)
- **基础URL**: `https://solsniffer.com/api/v2/`
- **完整URL**: `https://solsniffer.com/api/v2/token/{address}`
- **返回格式**: `tokenData.score` (不是 `data.score`)
- **API密钥**: `0112paiut0y6hqvpkv5eqfpafmtp4b`

**需要修复的问题**:
1. ❌ URL路径错误：应该是 `/token/{address}` 而不是 `/tokens/{chain}/{address}`
2. ❌ 返回字段错误：应该是 `tokenData.score` 而不是 `data.score`
3. ⚠️ API密钥传递方式未知（需要测试header或query参数）

## 功能实现状态总结

✅ **已完整实现的功能**:
1. 数据模型支持（TokenMetrics, FilterConfig）
2. API调用实现（SolSniffer, TokenSniffer）
3. 过滤器逻辑（filters.py）
4. Bot命令支持（/filter 命令）
5. State管理（保存/加载）
6. 消息显示（推送消息包含评分）

⚠️ **需要验证的项目**:
1. API响应格式是否与代码假设一致
2. 环境变量是否已配置
3. 实际运行时是否有错误

## 下一步行动

1. 检查是否有实际运行错误或日志
2. 验证API响应格式
3. 提供配置指南
