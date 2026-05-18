# 代理配置指南 - 日本代理池 (Coupon 触发)

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                    gen_pp_link.py                               │
│                    代理配置: 17912                               │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Clash Verge                                   │
│                    listeners: 17912 → JP-Exit                   │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    JP-Exit 代理组                                │
│                    ├── JP-Dedicated-F3-1                        │
│                    ├── JP-Dedicated-F3-2                        │
│                    ├── ...                                      │
│                    └── JP-Dedicated-F3-8                        │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    出口 IP: JP (日本)                            │
│                    触动 Coupon: Yes                              │
└─────────────────────────────────────────────────────────────────┘
```

## 已完成的配置

### 1. Clash 配置文件 (当前使用: glados)

**文件**: `C:\Users\29514\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev\profiles\RqT4lTbvcjdZ.yaml`

**添加的内容**:

```yaml
# 日本代理池专用端口 (Stripe/OpenAI Coupon 触发)
listeners:
  - name: jp-exit
    type: mixed
    port: 17912
    proxy: JP-Exit

# 日本代理组
proxy-groups:
  - name: JP-Exit
    type: "select"
    proxies:
      - JP-Dedicated-F3-1
      - JP-Dedicated-F3-2
      - JP-Dedicated-F3-3
      - JP-Dedicated-F3-4
      - JP-Dedicated-F3-5
      - JP-Dedicated-F3-6
      - JP-Dedicated-F3-7
      - JP-Dedicated-F3-8

# Stripe/OpenAI 走日本代理池
rules:
  - DOMAIN-SUFFIX,stripe.com,JP-Exit
  - DOMAIN-SUFFIX,stripe.network,JP-Exit
  - DOMAIN-SUFFIX,openai.com,JP-Exit
  - DOMAIN-SUFFIX,chatgpt.com,JP-Exit
  - DOMAIN-SUFFIX,auth.openai.com,JP-Exit
  - DOMAIN-SUFFIX,openaiapi-site.azureedge.net,JP-Exit
```

### 2. 项目配置文件

**文件**: `config.json`

```json
{
  "proxy": {
    "default": "socks5h://127.0.0.1:17912"
  },
  "paypal": {
    "auto_generate": true,
    "proxies": ["socks5h://127.0.0.1:17912"],
    "stage_proxies": {
      "checkout": "socks5h://127.0.0.1:17912",
      "stripe_init": "socks5h://127.0.0.1:17912",
      "payment_method": "socks5h://127.0.0.1:17912",
      "confirm": "direct"
    }
  }
}
```

**文件**: `sms_tool/gen_pp_link.py`

```python
PP_PROXIES = [
    "socks5h://127.0.0.1:17912",  # JP exit, required for coupon-qualified checkout
]
```

## 代理流程详解

| 阶段 | 代理端口 | 出口 IP | 原因 |
|------|----------|---------|------|
| ChatGPT Checkout | 17912 | JP (日本) | 触发 coupon 资格的关键 |
| Stripe Init | 17912 | JP (日本) | 生成 init_checksum + expected_amount |
| Stripe PaymentMethod | 17912 | JP (日本) | type=paypal |
| Stripe Confirm | 直连 | 本地 IP | 最后跳转是 pm-redirects.stripe.com/authorize/... |

## 验证配置

### 方法 1: 使用验证脚本

```bash
cd F:\epsoft\GPT-Register-Tool
python verify_proxy.py
```

期望输出:
```
[1] 测试端口 17912 (日本代理池)...
  [OK] 连接成功
  IP: xxx.xxx.xxx.xxx
  国家: JP
  城市: Tokyo
  [OK] 日本出口 - 可以触发 Coupon
```

### 方法 2: 使用 curl 命令

```bash
# 测试日本代理池
curl -s --proxy socks5h://127.0.0.1:17912 https://ipinfo.io/json | grep -E '"ip"|"country"'

# 期望输出:
# "ip": "xxx.xxx.xxx.xxx",
# "country": "JP"
```

### 方法 3: 测试 Stripe 访问

```bash
# 测试 Stripe 是否可以访问
curl -s --proxy socks5h://127.0.0.1:17912 https://api.stripe.com/v1/payment_methods -H "Authorization: Bearer test" | head -20
```

## Clash Verge 操作

### 应用配置

1. 打开 **Clash Verge** 应用
2. 点击左侧 **"配置"** 菜单
3. 找到配置文件 `glados` (RqT4lTbvcjdZ)
4. 点击 **"编辑"** 或 **"应用"**
5. 等待配置加载完成

### 查看监听端口

1. 在 Clash Verge 中，点击 **"日志"** 菜单
2. 查找类似以下日志:
   ```
   Mixed(http+socks) listening at: 127.0.0.1:17912
   ```

### 切换日本节点

如果需要手动切换日本节点:

1. 点击左侧 **"代理"** 菜单
2. 找到代理组 **"JP-Exit"**
3. 选择:
   - `JP-Dedicated-F3-1`
   - 或 `JP-Dedicated-F3-2`
   - 等等...

## 故障排除

### 问题 1: 端口 17912 无法连接

**症状**: `curl: (7) Failed to connect to 127.0.0.1 port 17912`

**解决**:
1. 确保 Clash Verge 已启动
2. 检查配置文件是否已应用
3. 查看 Clash 日志是否有错误
4. 尝试重启 Clash Verge

### 问题 2: 代理出口不是日本

**症状**: `ipinfo.io` 显示非 JP 国家

**解决**:
1. 检查 JP-Exit 代理组是否选择日本节点
2. 尝试切换到另一个日本节点
3. 检查 Clash 规则是否正确配置

### 问题 3: Coupon 不生效

**症状**: Stripe checkout 金额不为 0

**解决**:
1. 确保整个流程 (checkout -> stripe_init -> pm) 都走日本出口
2. 清除浏览器缓存和 Cookie
3. 尝试使用新的 ChatGPT 账号
4. 检查 Stripe init 响应中的 `amount_due` 和 `tax_amounts`

### 问题 4: listeners 配置不生效

**症状**: Clash 日志中没有显示 17912 端口

**解决**:
1. 检查 YAML 语法是否正确
2. 确保 `listeners` 部分在正确的位置
3. 尝试使用其他端口 (如 17913)
4. 查看 Clash 版本是否支持 `listeners` 功能

## 备用方案

如果 `listeners` 不生效，可以使用以下方案:

### 方案 1: 使用规则路由

在 Clash 规则中添加:
```yaml
rules:
  - DOMAIN-SUFFIX,stripe.com,JP-Exit
  - DOMAIN-SUFFIX,openai.com,JP-Exit
  - DOMAIN-SUFFIX,chatgpt.com,JP-Exit
```

然后使用端口 7897:
```json
{
  "proxy": {
    "default": "socks5h://127.0.0.1:7897"
  }
}
```

### 方案 2: 使用 Clash API 切换节点

```bash
# 切换到日本节点
curl -X PUT http://127.0.0.1:9090/proxies/Default%20Proxy \
  -H "Content-Type: application/json" \
  -d '{"name":"JP-Dedicated-F3-1"}'
```

## 文件清单

| 文件 | 说明 |
|------|------|
| `config.json` | 项目代理配置 |
| `sms_tool/gen_pp_link.py` | PayPal 链接生成器代理配置 |
| `verify_proxy.py` | 代理验证脚本 |
| `PROXY_GUIDE.md` | 本文档 |
| `Clash 配置文件` | 日本代理池配置 |

## 技术细节

### 代理协议

- **协议**: SOCKS5
- **地址**: 127.0.0.1
- **端口**: 17912
- **DNS**: 通过代理解析 (h 后缀)

### 代理组

- **名称**: JP-Exit
- **类型**: select (手动选择)
- **节点**:
  - `JP-Dedicated-F3-1` ~ `JP-Dedicated-F3-8` (GLaDOS 日本节点)

### 规则匹配

- `DOMAIN-SUFFIX,stripe.com,JP-Exit` - 所有 Stripe 子域名
- `DOMAIN-SUFFIX,openai.com,JP-Exit` - 所有 OpenAI 子域名
- `DOMAIN-SUFFIX,chatgpt.com,JP-Exit` - 所有 ChatGPT 子域名

## 更新日志

- **2026-05-18**: 初始配置
  - 添加 JP-Exit 代理组
  - 添加 17912 端口监听器
  - 添加 Stripe/OpenAI 规则
  - 更新项目配置文件
