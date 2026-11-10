# 云端接口逆向 · YunMu / iyunmu

本文记录 2026-11 对「控控乐」微信小程序（appid `wx69980405ad3c47f6`，版本 34）及其插件
`ymdev`（`wxb3c93e380e7e77df`）的逆向结果。所有报文均来自真机抓包（Reqable + 系统 CA），
请求与响应都已实际解密验证。

> 凭据（`x-api-key` / `x-api-secret`）**不写入仓库**。脚本从环境变量
> `YUNMU_API_KEY` / `YUNMU_API_SECRET` 读取。文中的密钥、sign、access_key 已脱敏。

---

## 1. 两条链路

小程序把设备操作拆成两层：

```
① 业务 REST：https://simsim.iyunmu.com        （Bearer JWT，账号态）
      ├─ 设备列表 / 区域 / 详情
      ├─ getMeterInfoBLE      → 云端生成"命令描述"（cmdType/ver/params/access_key/sign/timeStamp）
      └─ syncSocketData       → 读表前的会话/数据同步
② 设备命令：https://mqtt-v2.iyunmu.com        （x-api-key/secret，插件态）
      ├─ getMacBySn           → SN 换 MAC
      └─ bleCommandV1         → 下发 BLE payload；回传 BLE 响应换取解析结果
```

设备列表中 `"ble_prot_cmd": "ymHttpBleCommandV2"` 决定走 ② 的 V2 命令。

---

## 2. 业务 REST

请求头（微信内 H5 风格）：

```
Terminal-Type: innerH5
Content-Type: application/json;charset=UTF-8
Applet: <小程序 applet 标识>
Authorization: Bearer <JWT>
```

部分接口另外使用请求体加密（`X-Encrypted-Data` / `X-IV` / `X-Nonce` /
`X-Session-Key-ID` / `X-Sign` / `X-Timestamp`），密钥由 `/user/exchangeKey/` 交换，
本次未逆；不影响 BLE 命令链路。

已确认的接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/device/units/unitTree/` | 区域树 + `deviceIds` / `deviceSns` |
| GET | `/device/devicetouser/basicDetail/` | 设备基础列表（含 `sn`、`mac`、`ble_prot_cmd`、`dev_type`） |
| POST | `/device/v3/getMeterInfoBLE/` | 入参 `{"sn": ...}`，返回 BLE 命令描述 |
| POST | `/device/v3/syncSocketData/` | 读表前同步，返回加密数据 |
| GET | `/device/v3/socketInfo/` | 插座实时信息（电压/电流/功率/累计） |
| GET | `/device/detail/` | 设备详情 |

---

## 3. 设备命令 API（插件）

### 3.1 `POST /api/v1/getMacBySn`

```http
POST https://mqtt-v2.iyunmu.com/api/v1/getMacBySn
content-type: application/json
x-api-key: <key>
x-api-secret: <secret>
sign: <Base64(sha256(...))>
timestamp: <毫秒>
version: 2

{"sn":"KK01264I12F4"}
```

响应中的 `data.ciphertext` 用第 4 节的算法解密，得到 `{"sn":..., "mac":...}`。

### 3.2 `POST /api/v1/bleCommandV1`

请求头：

```
content-type: application/json
platform: WX
version: 5
x-api-key: <key>
x-api-secret: <secret>
x-api-sn: <SN>
x-api-timestamp: <unix 秒>
```

请求体：`{"ciphertext": "<Base64(hex(interleaved AES-CBC))>"}`。

解密后的请求（1 号机真实样本，字段已脱敏）：

```json
{
  "cmdType": "0x08",
  "ver": "1",
  "params": {"CRC16": "0"},
  "access_key": "<access key>",
  "sign": "<server-issued>",
  "timeStamp": 1794245652,
  "forge_flag": false,
  "isRes": 0,
  "callback": "http://10.0.0.178:11022/device/v3/syncSocketDataBack/KK01264I12F4/1/",
  "devCode": [ ...43 bytes... ],
  "macList": [],
  "sn": "KK01264I12F4",
  "test": false
}
```

响应解密后：

```json
{"code": "0F60FE0F6", "nonce_str": 57274840, "txMsgId": 82}
```

`code` 就是 BLE 读命令的 payload 编码（见第 5 节）。

### 3.3 响应回传（`isRes = 1`）

BLE 收到通知、剥帧后的原始 payload 原样上传：

```json
{
  "cmdType": "0x08",
  "ver": "1",
  "pkt": [36, 49, 44, ...],
  "isRes": 1,
  "access_key": "<access key>",
  "timeStamp": 1794245652,
  "sign": "Base64(sha256(反向 pkt hex + ts + access_key + sn[2:] + hex(ts)))",
  "txMsgId": 82,
  "callback": "...",
  "devCode": [ ... ],
  "macList": [],
  "sn": "KK01264I12F4"
}
```

云端解析后返回明文数据：

```json
{
  "cmdType": "0x08", "current": 0, "pf": 0, "power": 0,
  "power_down_limit": 0, "power_up_limit": 2000, "result": 0,
  "soft_ver": 5, "total_amount": 0.05, "total_time": 2037,
  "total_value": 0.11, "txMsgId": 82, "unitPrice": 0.5,
  "valid_power": 0, "voltage": 232.7
}
```

`isRes=1` 的 `sign` 可在本地计算（见 `scripts/cloud_crypto.py:response_sign`，已用抓包验证）。

---

## 4. 请求/响应加密

```
key_hex = SHA256( (x-api-secret + hex(ts) + x-api-key + sn[2:] + hex(ts)).upper() )
              .hexdigest().upper()
```

- 算法：AES-128-CBC，PKCS#7 填充；
- IV：16 个随机大写字母（ASCII），随包发送；
- 交错：`iv[0], c[0], iv[1], c[1], ..., iv[15], c[15]`，之后是剩余密文；
- 外层：`Base64( 十六进制字符串 )`（注意不是 `Base64(原始字节)`）。

响应使用**同一个** `key_hex`（同一次请求的 `ts`）解密。

实现见 `scripts/cloud_crypto.py`，已对真实抓包双向验证。

---

## 5. `code` → BLE payload

云端返回的 `code` 是十六进制字符串，按 **3 个字符一组**：

```
payload[i] = parseInt(group[i], 16) - txMsgId
组顺序需要反转（reverse）后再逐组相减
```

1 号机实测：`code = "0F60FE0F6"`，`txMsgId = 82 (0x52)`

```
0F6 → 0x0F6 - 82 = 0xA4
0FE → 0x0FE - 82 = 0xAC
0F6 → 0x0F6 - 82 = 0xA4
payload = a4 ac a4
```

与设备实测读命令完全一致。payload 形态恒为 `[s, s^0x08, s]`。

> `code` 的长度是 9 个字符（3 组），组值最大 0xFFF，因此 `s + txMsgId <= 0xFFF`。

---

## 6. 签名与重放限制

- `isRes=0`（下发命令）的 `sign` 由 REST 侧（`getMeterInfoBLE` / `syncSocketData`）
  生成，与 `access_key + 时间戳 + 设备` 绑定；
- 直接伪造会得到：

| 情况 | 返回 |
|---|---|
| 时间戳过旧 | `{"code":400,"msg":"设备时间与服务器时间不一致，请校准系统时间"}` |
| 新时间戳 + 旧签名 | `{"code":400,"msg":"无效请求"}` |

- 因此**不能**在本地为任意 SN 生成新命令；要获取某台设备的 `code`，必须由小程序对该设备
  触发一次 `bleCommandV1`（BLE 连接失败也会先发云端请求）；
- `Authorization: Bearer <JWT>` 由微信登录态换取，未逆；Reqable 默认将其脱敏为 `****`。

---

## 7. 复现用命令

```bash
export YUNMU_API_KEY=...      # 从插件包中提取的硬编码 user
export YUNMU_API_SECRET=...

# 解密一条已抓到的 bleCommandV1 响应
python scripts/cloud_crypto.py --sn KK01264I12F4 --ts 1794245655 \
  --decrypt '<ciphertext>'
```

注意：`--ts` 用请求头 `x-api-timestamp`（秒），不是请求体里的 `timeStamp` 字段；
两者在样本中相差 3 秒，但密钥只用头部时间戳。
