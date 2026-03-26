# VPS Deploy Commands (Ubuntu 22.04+)

## 1) Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

## 2) Upload project and create venv

```bash
mkdir -p /opt/video-generator
cd /opt/video-generator
# Скопируй сюда файлы проекта (git clone/scp)

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3) Environment file

```bash
cp .env.example .env
nano .env
```

Заполни минимум:
- `PROXYAPI_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_ID`
- `TELEGRAM_ADMIN_CHAT_IDS`
- `BILLING_DB_PATH` (например `/opt/video-generator/billing.db`)
- `WEB_ADMIN_TOKEN`

## 4) systemd units

Скопируй unit-файлы из `deploy/systemd/` в `/etc/systemd/system/`:

```bash
sudo cp deploy/systemd/video-web.service /etc/systemd/system/
sudo cp deploy/systemd/video-bot.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable video-web video-bot
sudo systemctl start video-web video-bot
```

После запуска привяжи web-токен к пользователю в боте:

```text
/set_web_token <chat_id> <token>
```

Проверка:

```bash
sudo systemctl status video-web --no-pager
sudo systemctl status video-bot --no-pager
journalctl -u video-web -n 100 --no-pager
journalctl -u video-bot -n 100 --no-pager
```

## 5) Nginx reverse proxy

Создай `/etc/nginx/sites-available/video-web`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Активируй:

```bash
sudo ln -s /etc/nginx/sites-available/video-web /etc/nginx/sites-enabled/video-web
sudo nginx -t
sudo systemctl restart nginx
```

Для HTTPS:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```
