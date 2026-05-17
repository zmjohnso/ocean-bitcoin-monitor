#!/bin/bash
# One-time VPS setup for ocean-bitcoin-monitor
set -e

echo "=== Ocean Bitcoin Monitor Setup ==="

# 1. Install Python dependencies
pip3 install -r requirements.txt

# 2. Create .env from template if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env — fill in your TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, and WALLET before running."
    echo ""
fi

# 3. Ensure logs directory exists
mkdir -p logs

# 4. Test run (debug mode — no alerts sent)
echo "Running in debug mode to verify scraping..."
python3 monitor.py --debug

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Telegram token, chat ID, and wallet address"
echo "  2. Run: python3 monitor.py --debug   (to verify before going live)"
echo "  3. Add to crontab (crontab -e):"
echo "     */5 * * * * /usr/bin/python3 $(pwd)/monitor.py >> $(pwd)/logs/monitor.log 2>&1"
echo ""
echo "Telegram bot setup:"
echo "  1. Message @BotFather on Telegram → /newbot → follow prompts → copy token"
echo "  2. Send any message to your new bot"
echo "  3. Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
echo "     Find 'chat' → 'id' in the JSON response — that's your TELEGRAM_CHAT_ID"
