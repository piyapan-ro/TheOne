# AI Trader (XAUUSD × MT5 × Claude/ChatGPT)

| โฟลเดอร์ | เนื้อหา |
|---|---|
| `dashboard/` | AI Trader Dashboard (FastAPI + MT5) – ดู `dashboard/README.md` |
| `EA/` | GoldScalper_M1.mq5, AIScalper_M1.mq5 (MetaTrader 5 Expert Advisors) |
| `training/` | train_ai_scalper.py – เทรนโมเดล ONNX ให้ AIScalper |

## ติดตั้งบนเครื่องใหม่
```
git clone https://github.com/<you>/ai-trader.git
cd ai-trader\dashboard
install.bat
start.bat
```
ค่าตั้ง/API Key ไม่ได้อยู่ใน Git (ถูก ignore ไว้) – ต้องใส่ใหม่ในหน้า Settings ของ Dashboard

## อัปเดตโค้ด
```
git pull
```
