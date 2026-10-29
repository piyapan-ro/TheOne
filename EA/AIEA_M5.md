# AI EA M5 – ออกแบบระบบ

EA เทรดทองคำบน M5 ที่ให้ AI ตัดสินใจฝั่ง แต่ EA เป็นคนตรวจและส่งคำสั่งเอง เข้าครั้งละ 1 ไม้ มี SL/TP ทุกไม้

```
MT5: AIEA_M5.mq5                         Dashboard (start.bat)                    AI (Claude / ChatGPT)
───────────────────                      ──────────────────────                   ─────────────────────
แท่ง M5 ปิด → วิเคราะห์เอง                 POST /api/ea/decide
  แนวรับ–ต้าน (M5+H1), OB, โครงสร้าง  ──►   ตรวจงบเครดิตวันนี้          ──►       ให้คะแนน BUY / SELL แยกกัน
  BOS / CHOCH / Sweep, บริบท H1/H4         เรียก AI (JSON schema บังคับ)  ◄──       + setup + zone_id
คัดกรองก่อนเรียก AI (ประหยัดเครดิต)          ตรวจคำตอบด้วยกฎตายตัว
                                     ◄──   ส่งแผน BUY / SELL / NONE กลับ
ยืนยันด้วยแท่ง M5 ที่ปิดแล้ว → Market order
SL/TP/Lot/Trailing                   ──►   POST /api/ea/report (นับต้นทุนต่อออเดอร์)
```

API Key อยู่ใน Dashboard เท่านั้น EA ไม่ถือ Key

## 1. ข้อมูลที่ส่งให้ AI (ทุกค่ามาจากแท่งที่ปิดแล้ว)

| ส่วน | รายละเอียด |
|---|---|
| `m5_bars_ohlc` | 36 แท่ง M5 ล่าสุด (ตั้งได้ `InpSendBars`) |
| `zones` | โซนละไม่เกิน 3 ต่อชนิด มี `id` (S1, R1, BullOB1, BearOB1 …), `kind`, `tf`, `low/high`, `touches`, `age_bars`, `flip` (`broken_up` / `broken_down` ถ้าแท่ง M5 ปิดทะลุใน 6 แท่งล่าสุด) |
| แนวรับ–ต้าน | กลุ่มของ swing high/low บน M5 และ H1 ที่ราคาใกล้กัน |
| Order Block | แท่งสวนทางแท่งสุดท้ายก่อนแรงพุ่ง ≥ 1 ATR ที่ยังไม่ถูกปิดทะลุ |
| `structure` (M5) | เทรนด์, swing ล่าสุด, BOS, CHOCH/MSS, liquidity sweep (buy_side / sell_side) |
| `h1`, `h4` | EMA50/200, เทรนด์จาก EMA และจากโครงสร้าง, ATR, swing, BOS/CHOCH ล่าสุด |

## 2. การเลือกฝั่งและแผนเข้า

AI ตอบเป็น JSON ตาม schema (ระบบบังคับรูปแบบ AI ตอบนอกรูปแบบไม่ได้):

```json
{"buy":  {"score": 0-100, "setup": "breakout|retest|smc_retest|none", "zone_id": "S1", "reason": "..."},
 "sell": {"score": 0-100, "setup": "...", "zone_id": "...", "reason": "..."},
 "decision": "BUY|SELL|NONE", "valid_bars": 1-12, "note": "สรุปสั้น ๆ"}
```

คะแนนเป็นน้ำหนักเปรียบเทียบของหลักฐาน ไม่ใช่เปอร์เซ็นต์โอกาสชนะ

Dashboard ตรวจคำตอบซ้ำด้วยกฎตายตัว (`dashboard/backend/ea_ai.py` → `finalize`) ถ้าไม่ผ่านจะได้ NONE:

- ฝั่งที่เลือกต้องได้ ≥ `min_score` (60) และนำอีกฝั่ง ≥ `min_gap` (15) ไม่งั้นถือว่าคะแนนต่ำหรือสูสี
- `zone_id` ต้องมีอยู่จริงในรายการที่ EA ส่งไป AI สร้างราคาเองไม่ได้
- BUY ใช้ได้เฉพาะ `support` / `bull_ob`, SELL ใช้ได้เฉพาะ `resistance` / `bear_ob`
- **breakout / breakdown**: โซนต้องถูกแท่ง M5 ที่ปิดแล้วทะลุจริง (`flip`)
- **retest**: เทียบโซนฝั่งเดียวกัน (แนวรับกับ Bullish OB, แนวต้านกับ Bearish OB) แล้วเลือกจุดรอที่ใกล้ราคากว่า และต้องอยู่ไม่เกิน `max_wait_atr` (2 ATR)
- **SMC retest**: ต้องมี liquidity sweep ฝั่งตรงข้ามเกิดก่อน CHOCH/MSS ในทิศเดียวกันอยู่ในข้อมูลโครงสร้าง
- จุดเข้า = ขอบโซน (BUY = ขอบบน, SELL = ขอบล่าง) จุด invalidation = ขอบอีกด้าน

## 3. การยืนยันก่อนส่ง Market order (ใน EA)

ระหว่างรอ แผงบนกราฟจะแสดง **AI WAIT** พร้อมเหตุผล

1. **แท่ง M5 ที่ปิดแล้ว** ยืนยัน setup
   - breakout: ปิดเหนือ/ใต้โซน ≥ 0.1 ATR เป็นแท่งตามทิศ
   - retest / SMC retest: แตะโซนแล้วปิดกลับเกินกลางโซนเป็นแท่งตามทิศ
2. **ความสดของสัญญาณ**: แท่งยืนยันต้องเป็น 1–2 แท่งล่าสุด (`InpSignalMaxAgeBars`) และแผนหมดอายุเมื่อเกิน `valid_bars`
3. **ระยะห่างจากจุดเข้า**: ราคาต้องห่างจุดเข้าไม่เกิน 0.5 ATR (`InpMaxEntryDistAtr`) ถ้าวิ่งไปไกลแล้วจะไม่ไล่ราคา
4. **ความถูกต้องของ setup**: แท่งปิดทะลุโซนฝั่งผิด = ยกเลิกแผน, spread ต้องไม่เกินกำหนด, SL ต้องไม่กว้างเกิน 3 ATR

## 4. การจัดการความเสี่ยง

- Lot จาก Risk % ของ Equity (หรือ Lot คงที่) จำกัด `InpMaxLot` และลดลงอัตโนมัติถ้า margin ไม่พอ
- SL = ขอบ invalidation ± 0.3 ATR (ขั้นต่ำ 0.8 ATR, เกิน 3 ATR ไม่เข้า) · TP = SL × `InpRR` (2.0)
- Breakeven ที่กำไร 1R, Trailing เริ่มที่ 1.5R ห่าง 1 ATR (SL ขยับไปทางกำไรเท่านั้น)
- ครั้งละ 1 ไม้ ขาดทุนต่อวันสูงสุด 3% จำนวนไม้ต่อวัน และช่วงเวลาเทรด
- **AI ผิดพลาดหรือเชื่อมต่อไม่ได้** (Dashboard ไม่เปิด, token ผิด, timeout, AI ปฏิเสธ, JSON ผิด, งบหมด): แสดง **AI OFFLINE** และงดเปิดออเดอร์ใหม่ `InpRetryMinutes` นาที ไม้ที่เปิดอยู่ยังมี SL/TP บนเซิร์ฟเวอร์และ trailing ทำงานต่อ

## 5. ต้นทุนเครดิต (เป้าหมาย ≈ 0.02 USD ต่อออเดอร์)

EA ไม่เรียก AI ทุกแท่ง จะเรียกเมื่อเงื่อนไขครบเท่านั้น:

- มีแท่ง M5 ใหม่ปิด, ไม่มีไม้เปิดอยู่, ไม่มีแผนที่รออยู่
- ราคาอยู่ใกล้โซน ≤ 1.5 ATR หรือมี BOS / CHOCH / sweep / ทะลุโซนใน 6 แท่งล่าสุด
- ห่างจากครั้งก่อน ≥ 3 แท่ง, หลัง AI ตอบ NONE พัก 3 แท่ง, ไม่เกิน 40 ครั้งต่อวัน
- Dashboard มีงบต่อวัน `daily_budget_usd` (1 USD) เมื่อใช้ครบจะหยุดเรียก AI (= ไม่เปิดไม้ใหม่)

ข้อมูลที่ส่งเป็น JSON บรรทัดเดียวประมาณ 2,500–3,500 tokens คำตอบสั้น (effort `low`)
ประมาณการต่อครั้ง: Claude Sonnet 5.5 ≈ 0.01 USD, Claude Haiku 5.5 ≈ 0.0005 USD ถ้าเรียก 2 ครั้งต่อ 1 ออเดอร์ Sonnet จะอยู่ราว 0.02 USD
ต้นทุนจริงคำนวณจาก token ที่ AI รายงานกลับทุกครั้ง ดูได้ที่ `GET /api/ea/stats` (ต้นทุนต่อครั้ง, ต่อออเดอร์, รายวัน และ log คำตอบ 300 ครั้งล่าสุด)

## 6. สิ่งที่วาดบนกราฟ (ครบตามแบบข้อ 1–5)

**บนกราฟ**
- กล่องโซน: Support (เขียว) / Resistance (แดง) / Bullish OB / Bearish OB ของ M5 และ H1 พร้อมป้าย id, TF, ชนิด, จำนวนครั้งที่แตะ, BROKEN UP/DOWN
- โครงสร้าง M5: เส้น BOS (ฟ้า), CHOCH (ส้ม), ป้าย SWEEP ตรงจุดกวาด liquidity, เส้น swing high/low ล่าสุด (= liquidity ฝั่ง buy/sell)
- บริบท H1/H4: เส้น H1 swing high/low, H1 EMA50, H4 EMA50/EMA200 (เฉพาะที่อยู่ใกล้ราคา)
- แผนของ AI ที่รออยู่: กรอบทองรอบโซนที่เลือก, เส้น entry (ทอง), เส้น invalidation (แดง) และป้าย "farther - not chosen" ที่โซนฝั่งเดียวกันซึ่งไกลกว่า (ผลการเปรียบเทียบ retest)
- จุดที่ EA เปิดออเดอร์ตามแผน AI: ป้าย ^BUY / vSELL พร้อม setup และคะแนน

**แผงสถานะมุมซ้ายบน**
| ส่วน | แสดง |
|---|---|
| หัว | สถานะ AI READY / IDLE / WAIT / NONE / BUY / SELL / OFFLINE / PAUSED, symbol, ATR, spread, เวลา server |
| CONTEXT | H4 และ H1: เทรนด์ EMA, โครงสร้าง, BOS/CHOCH ล่าสุด · M5: โครงสร้าง, เหตุการณ์ล่าสุด, sweep, จำนวนโซน |
| AI | เวลาและรุ่นที่ตอบ, คะแนน BUY และ SELL แยกกัน พร้อม setup / โซน / เหตุผลของแต่ละฝั่ง, แผนที่เลือก (ฝั่ง setup entry โซน invalidation อายุ) และ note |
| CONFIRMATION | เช็กลิสต์ 4 ข้อก่อนส่ง Market order: แท่ง M5 ที่ปิดแล้วยืนยัน / ระยะห่างจากจุดเข้า / setup ยังถูกต้อง / สัญญาณยังสด แต่ละข้อแสดง [v]/[x] และรายละเอียด |
| RISK | ออเดอร์ถัดไป: lot (จาก risk %), SL (กี่ ATR), TP, R:R, จุด breakeven/trailing · ไม้ที่เปิดอยู่: ราคาเข้า SL TP กำไรเป็น R และสถานะ BE/trailing |
| AI CALLS & COST | จำนวนครั้ง/วัน, ค่าใช้จ่ายวันนี้, งบของ Dashboard, เหตุผลที่กำลังเรียก/ไม่เรียก AI และข้อความ AI OFFLINE พร้อมเวลาที่จะลองใหม่ |

ปิดได้ด้วย `InpDrawZones` / `InpDrawPanel` สีปรับได้ในกลุ่ม Chart ถอด EA ออกแล้ว object ทั้งหมดถูกลบ

## 7. ทดสอบใน Strategy Tester

Strategy Tester ของ MT5 ไม่อนุญาตให้ใช้ WebRequest EA จึงเรียก AI ตรง ๆ ใน tester ไม่ได้ ตั้ง `InpTesterMode` ได้ 3 แบบ (บนกราฟจริงค่านี้ไม่มีผล EA ใช้ AI เสมอ):

| โหมด | ทำอะไร | ใช้เมื่อ |
|---|---|---|
| **RULES** (ค่าเริ่มต้น) | EA ให้คะแนน BUY/SELL เองจากกฎในตัว (เทรนด์ H4/H1, โครงสร้าง M5, CHOCH, sweep, โซน, ระยะถึงโซนตรงข้าม) แล้วเดินผ่านการยืนยัน/SL/TP/trailing ชุดเดียวกับตอนใช้ AI | ทดสอบระบบจัดการความเสี่ยงและพารามิเตอร์ ฟรี เร็ว |
| **EXPORT** | เขียนข้อมูลที่จะส่งให้ AI ลงไฟล์ `Common\Files\AIEA_requests.jsonl` ทีละแท่ง ไม่เปิดออเดอร์ | ขั้นแรกของการ backtest ด้วย AI จริง |
| **REPLAY** | อ่านคำตอบ AI จาก `Common\Files\AIEA_replay.tsv` แล้วเทรดตามนั้น | ขั้นสุดท้ายของการ backtest ด้วย AI จริง |

ขั้นตอน backtest ด้วย AI จริง (เสียเครดิตรอบแรกครั้งเดียว รันซ้ำเพื่อปรับ SL/TP/lot ไม่เสียเพิ่ม):

1. รัน tester ด้วย `InpTesterMode = EXPORT` ช่วงเวลาที่ต้องการ (เงื่อนไขคัดกรองก่อนเรียก AI ทำงานเหมือนจริง จึงได้เฉพาะแท่งที่ EA จะถาม AI จริง ๆ)
2. ใน `dashboard\backend` รัน (Dashboard ต้องมี API Key แล้ว):
   ```
   ..\.venv\Scripts\python ea_backtest.py "%APPDATA%\MetaQuotes\Terminal\Common\Files\AIEA_requests.jsonl" "%APPDATA%\MetaQuotes\Terminal\Common\Files\AIEA_replay.tsv" --max-cost 3
   ```
   สคริปต์ถาม AI แท่งละครั้ง พิมพ์ต้นทุนสะสม หยุดเมื่อถึง `--max-cost` (รันซ้ำเพื่อทำต่อได้ ไม่ถามแท่งเดิมซ้ำ) ใส่ `--model claude-haiku-5-5` ถ้าต้องการรอบถูก ๆ ก่อน
3. รัน tester ด้วย `InpTesterMode = REPLAY` ช่วงเวลาเดียวกัน

ข้อควรระวัง: AI อาจจำราคาทองในอดีตได้จากข้อมูลที่เคยเรียน ผล backtest จึงอาจดีกว่าความจริง ใช้ RULES และการทดสอบบน Demo แบบเดินหน้าจริงประกอบด้วย

## 8. ติดตั้ง

1. เปิด Dashboard (`dashboard\start.bat`) และใส่ API Key ในหน้า Settings
2. MT5 → Tools → Options → Expert Advisors → ติ๊ก *Allow WebRequest for listed URL* แล้วเพิ่ม `http://127.0.0.1:8000`
3. คอมไพล์ `EA/AIEA_M5.mq5` ใน MetaEditor แล้วลากลงกราฟ XAUUSD (timeframe ใดก็ได้ EA อ่าน M5/H1/H4 เอง)
4. ไม่ต้องใส่ token: Dashboard เขียน token ให้ที่ `%APPDATA%\MetaQuotes\Terminal\Common\Files\AIEA_token.txt` และ EA อ่านเองเมื่อ `InpApiToken` ว่าง (ใส่ `InpApiToken` เฉพาะกรณี Dashboard อยู่คนละเครื่อง)
5. ทดสอบบนบัญชี Demo ก่อน (WebRequest ใช้ใน Strategy Tester ไม่ได้)

ค่าฝั่ง AI ปรับได้ที่ `POST /api/ea/config` (หรือแก้ `dashboard\data\ea_ai.json`): `model` (ว่าง = ใช้รุ่นเดียวกับหน้า Settings), `effort`, `min_score`, `min_gap`, `max_wait_atr`, `daily_budget_usd`
