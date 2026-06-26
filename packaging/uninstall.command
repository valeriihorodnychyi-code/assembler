#!/bin/bash
# Double-click to cleanly remove Assembler from this Mac.
cd "$(dirname "$0")"
echo "============================================"
echo "   Видалення Assembler"
echo "============================================"
echo "Буде видалено:"
echo "  • /Applications/Assembler.app"
echo "  • ~/.assembler  (код, лог, ключі цього Mac)"
echo ""
read -p "Продовжити? (y/n) " ans
if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
  echo "Скасовано."
  read -p "Enter, щоб закрити…"
  exit 0
fi

# stop any running copy
killall -9 Assembler 2>/dev/null
pkill -9 -f "server.app" 2>/dev/null

# remove the app (from Applications and next to this script, if present)
rm -rf "/Applications/Assembler.app"
rm -rf "$(pwd)/Assembler.app"

# remove per-machine data (code cache, logs, keys)
rm -rf "$HOME/.assembler"

echo ""
echo "✅ Assembler видалено."
echo "ℹ️  Моделі Whisper (~/.cache/huggingface) НЕ чіпали — це спільний кеш."
echo "   Якщо точно треба прибрати і їх:  rm -rf ~/.cache/huggingface"
echo ""
read -p "Enter, щоб закрити…"
