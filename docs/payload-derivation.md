# 每设备 payload / 去扰常量的来源（进行中）

## 结论摘要

- 读命令 payload 的**传输编码**已经完全解开（见 `cloud-api.md` §5）：云端下发 `code`，
  客户端按 3 hex 一组反转后减 `txMsgId` 得到字节。
- 每台设备底层那个字节 `s`（payload = `[s, s^0x08, s]`）**没有**在上游
  可观测字段（SN、MAC、云端设备 id、区域 id）上找到可复现的简单关系。
- 已排除的构造方式见下表。基于 4 台真实设备，以下算法全部**不成立**：
  CRC8（全部 255 个多项式 × init 0/0xFF × 反射/非反射）、CRC16（全部 65536 个
  多项式 × init 0/0xFFFF × 反射/非反射 × 输出低/高字节/异或折叠）、CRC32/Adler32、
  FNV-1/1a、djb2、sdbm、Java hash、MD5/SHA1/SHA256/SHA512/BLAKE2 的任意字节位、
  AES S-box、字节和/异或折叠。
- 现有证据更支持：`s` 与响应去扰常量是**出厂/绑定阶段写入云端的每设备密钥**，
  不是由公开标识符派生。要证伪需要更多设备样本或云端数据库访问。

## 数据集

真实设备，payload/去扰值均经实际读取验证（Pi 端连续采集正常）：

| 设备 | SN | BLE MAC | payload `s` | payload | 响应去扰 |
|---|---|---|---|---|---|
| 1 | KK01264I12F4 | `26:04:18:01:12:F4` | `0xA4` | `a4 ac a4` | `0x2C` |
| 2 | KK04261F2F96 | `26:01:15:04:2F:96` | `0x5A` | `5a 52 5a` | `0x0D` |
| 3 | KK01264I0311 | `26:04:18:01:03:11` | `0xD0` | `d0 d8 d0` | `0x79` |
| 4 | KK01264I0316 | `26:04:18:01:03:16` | `0xB0` | `b0 b8 b0` | `0x3F` |

平台侧已知标识（来自 `/device/devicetouser/basicDetail/` 与 `/device/detail/`）：

| SN | `id`（basicDetail） | `device`（detail） | `pin` |
|---|---|---|---|
| KK01264I12F4 | 1984007 | 1265025? | 未采集 |
| KK04261F2F96 | 2001508 | 1273842 | `3CP927` |
| KK01264I0311 | 2001516 | 1273846? | 未采集 |
| KK01264I0316 | 2001517 | 1273847? | 未采集 |

> `pin` 是关键候选字段（6 位出厂码），但只有 2 号机一个样本，不足以验证。

## 已验证的传输编码（非派生）

云端 `code`：

```
code = "0F60FE0F6", txMsgId = 82
groups = ["0F6","0FE","0F6"]  → reverse → 逐组 parseInt(hex) - txMsgId
payload = [0xA4, 0xAC, 0xA4]
```

`txMsgId` 每条命令递增，`s` 固定；同一设备重复读取时 `s` 不变。

## 已排除的假设（4/4 必须同时成立才算命中）

| 假设 | 搜索空间 | 结果 |
|---|---|---|
| `s` = CRC8(SN/MAC/变体) | 255 poly × init {0,255} × 反射 × 6–8 种输入 | 无 |
| `s` = CRC16(...) 低/高/折叠字节 | **全部 65536 个 poly** × init {0,0xFFFF} × 反射 × 输入变体 | 无 |
| `s` = CRC32/Adler/FNV/djb2/sdbm/Java | 各输出字节位 | 无 |
| `s` = MD5/SHA1/SHA256/SHA512/BLAKE2 任意字节 | 输入变体 | 无 |
| `s` = AES S-box(字节) | 输入变体各位置 | 无 |
| `s` = 字节和 / 异或折叠 | 输入变体 | 无 |
| `(s, xor)` = CRC16/hash 的 16 位组合 | 移位组合 | 无 |
| CRC8/CRC16 + 任意固定 `xorout` | 全部 poly，xorout 由 4 台一致性反推 | 无 |
| CRC16 使用 MAC 自身派生的 init/poly | 帧层同一套 `(init, poly)` | 无 |
| 哈希/CRC 带固定盐（`yunmu`, `iyunmu`, appid, 型号等 16 个候选） | MD5/SHA1/SHA256 × 字节位 × CRC8 全 poly | 无 |
| HMAC(variant1, variant2) | MD5/SHA1/SHA256 × 字节位 | 无 |
| AES-ECB(hash(variant)[:16], 固定/变体明文) | MD5/SHA1/SHA256 | 无 |

输入变体包括：SN 全串、`SN[2:]`、`SN[-4:]`、`SN[-4:]` 的 hex 字节、MAC 字节、
MAC 反序、MAC hex 串（大小写/带冒号）、MAC+SN、SN+MAC、`basicDetail` 的 `id`、
区域 `deviceIds` 的数字与 hex。

## 复现搜索

见 `scripts/search_payload_rule.py`（numpy 向量化 CRC16 全多项式搜索，几十秒内完成）。
命中判定：同一算法在 4 台设备上同时得到目标 `s` 或去扰值。

## 下一步（需要新信息）

1. **采集其余 3 台的 `pin`**（打开设备详情即可，详情接口明文返回），验证 `s = f(pin)`；
2. **同一设备的第二条命令**（设阈值/清零）的 payload，观察同一 `s` 是否被复用为
   命令掩码——若是，则"每设备一个密钥"得到佐证；
3. **云端数据库或接口**若能看到绑定/出厂字段，优先检查是否有 `ble_key` / `secret`
   一类字段；
4. 若都不成立，结论即为：每设备随机密钥，只存在于设备固件与云端，无法由
   SN/MAC 推导；本地侧必须按设备回放/记录该值（当前 `KNOWN_PROFILES` 的做法）。
