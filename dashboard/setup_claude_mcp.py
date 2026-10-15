"""Adds the 'ai-trader' MCP server to Claude Desktop's config (keeps everything else, makes a backup)."""
import json
import os
import shutil
import sys
import time

root = os.path.dirname(os.path.abspath(__file__))
python = os.path.join(root, ".venv", "Scripts", "python.exe")
server = os.path.join(root, "backend", "mcp_server.py")
import glob

if not os.path.exists(python):
    sys.exit("ไม่พบ .venv - กรุณารัน install.bat ก่อน")

# normal installer: %APPDATA%\Claude ; Microsoft Store / MSIX install: %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude
dirs = [os.path.join(os.environ["APPDATA"], "Claude")]
dirs += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"))


def patch(cfg_dir):
  cfg = os.path.join(cfg_dir, "claude_desktop_config.json")
  os.makedirs(cfg_dir, exist_ok=True)
  data = {}
  if os.path.exists(cfg):
      backup = cfg + time.strftime(".backup-%Y%m%d-%H%M%S")
      shutil.copy2(cfg, backup)
      print("สำรองไฟล์เดิมไว้ที่:", backup)
      try:
          with open(cfg, encoding="utf-8") as f:
              data = json.load(f)
      except Exception as e:
          print(f"อ่านไฟล์ config เดิมไม่ได้ ({e}) - ข้าม {cfg}")
          return
  data.setdefault("mcpServers", {})["ai-trader"] = {"command": python, "args": [server]}
  with open(cfg, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
  print("เพิ่ม MCP server 'ai-trader' แล้ว:", cfg)


for d in dirs:
    patch(d)
print("ขั้นต่อไป: ปิด Claude Desktop ให้สนิท (คลิกขวาไอคอนที่ taskbar > Quit) แล้วเปิดใหม่")
