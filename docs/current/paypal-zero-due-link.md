# PayPal 0 元直链（0元 + PayPal BA 授权链）

本文说明如何用分段代理 + 促销更新（`/checkout/update`）在**同一个 ChatGPT checkout 会话**里同时拿到 **0 元金额** 和 **PayPal 支付方式**，最终提取 PayPal BA 授权直链（`https://www.paypal.com/agreements/approve?ba_token=...`）。

## 背景：为什么需要促销更新阶段

- **PayPal 是否出现由 checkout 的账单地区决定**：只有 PayPal 支持区（US/GB/IE/DE/FR/AU/CA/NZ…）的 checkout，Stripe `payment_method_types` 才会包含 `paypal`；JP/SG/TH 只给 `card`，TR 直接 422。
- **0 元促销资格由「打促销时的出口 IP」决定**，与账单国无关：某些出口区（如 JP/TH/VN）能让 `plus-1-month-free` 生效变 0 元，另一些则维持全价。
- 单次 checkout 创建里，PayPal 区往往**不**给 0 元、给 0 元的区往往**不**支持 PayPal——看起来「互斥」。

**破解方式**（逆向自参考实现 `app.py` 与 `ideal-link-extractor`）：checkout 在 **PayPal 支持区**创建（保证 PayPal 可用），随后调用专用端点 **`POST /backend-api/payments/checkout/update`** 从**促销可用区出口**给**同一个 `cs_id`** 打促销，使其变 0 元。两者因此可以在同一会话共存。

> 实测验证：`checkout=US(账单US) → /checkout/update(JP 出口打促销) → stripe init(US)` 得到 `amount=0` 且 `payment_method_types=['card','paypal']`。

## 协议流程（`PPLinkExtractor.extract`）

```
1. create_checkout            (checkout 出口, 账单=PayPal 支持区)   -> cs_id
2. /checkout/update  +promo   (promotion 出口, 促销可用区)          -> 同一 cs 变 0元   [可选阶段]
   /checkout/taxes            (provider 出口, 可选)                 -> 同步账单/税区
3. stripe init                (provider 出口)                       -> 校验 amount==0 且 paypal 可用
4. create PayPal PM -> confirm -> approve -> poll                   -> 跟随跳转提取 BA 直链
```

相关端点：

| 端点 | 用途 |
| --- | --- |
| `POST /backend-api/payments/checkout` | 创建 checkout |
| `POST /backend-api/payments/checkout/update` | **对已有 cs 打促销（0 元）** |
| `POST /backend-api/payments/checkout/taxes` | 同步账单/税区（可选）|
| `POST /backend-api/payments/checkout/approve` | ChatGPT 侧批准 |

## 配置（`config.json` → `paypal`）

促销更新阶段是**可选（opt-in）**：不配 `promotion` 代理时行为与旧版完全一致。

```json
"paypal": {
  "stage_proxies": {
    "checkout":  "<PayPal 支持区代理, 如 US>",
    "provider":  "<PayPal 支持区代理, 如 US>",
    "approve":   "<PayPal 支持区代理, 如 US>",
    "promotion": "<促销可用区代理, 如 JP/TH/VN>"   // 留空 = 禁用促销更新阶段
  },
  "promotion_taxes": false,               // 是否额外调用 /checkout/taxes
  "promo_campaign_id": "plus-1-month-free"
}
```

也支持动态代理 API：在 `paypal.stage_proxy_api_urls.promotion` 放一个返回 `ip:port` 的接口。

## 命令行用法

单次模式（`sms_tool/gen_pp_link.py`）：

```bash
python -m sms_tool.gen_pp_link <ACCESS_TOKEN> \
  --checkout-proxy "<US 代理>" --provider-proxy "<US 代理>" --approve-proxy "<US 代理>" \
  --promotion-proxy "<JP/TH/VN 代理>" \
  --target US --checkout-country US --json
```

促销矩阵搜索（`PayPal 区 × promotion 区`，对齐参考实现的 zero-amount matrix，成功即停）：

```bash
python -m sms_tool.gen_pp_link <ACCESS_TOKEN> \
  --proxy-template "user-region-XX-sid-XXXX-t-5:pass@gate:443" \
  --target-countries US,DE,GB,AU,CA \
  --promotion-countries JP,TH,VN --json
```

主 CLI（`sms_tool/cli.py`）生成 BA 链：

```bash
python -m sms_tool --generate-ba-link --at <ACCESS_TOKEN> \
  --checkout-proxy <US> --provider-proxy <US> --approve-proxy <US> \
  --promotion-proxy <JP/TH/VN> --require-ba-token
```

## 返回结果

`generate_pp_link` / `PPLinkExtractor.extract` 结果新增 `promotion_proxy` 字段；矩阵模式返回附带 `matrix`（每个 `paypal_region × promotion_region` 组合的 `amount`/`link_type`/`status`）。

## 注意事项

- **账号促销资格**是按「账号 + billing + 出口区」组合判定的。请用促销资格正常的账号；不同账号的可用 promotion 区可能不同，建议用矩阵搜索找可用组合。
- **代理质量**：`approve` 阶段对出口 IP 敏感，劣质住宅 IP 可能被 Cloudflare 403。init 关卡（0 元 + PayPal 双条件）通过后，若 approve 反复失败，多为代理问题，换干净的 `provider/approve` 出口即可。
- **BA token 视为敏感信息**，日志中会脱敏；请勿完整记录。
- 促销更新失败是**非致命**的：会记录日志并交由 `require_zero` 关卡兜底（若最终非 0 元且要求 0 元，则在 init 处失败）。
