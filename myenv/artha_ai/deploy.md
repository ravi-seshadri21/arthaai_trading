# ARTHA AI — AWS EC2 Deployment Guide
# Free Tier: t2.micro, Ubuntu 22.04, 8GB storage

# ═══════════════════════════════════════════════════════════
# PART 1 — LAUNCH EC2 INSTANCE
# ═══════════════════════════════════════════════════════════

# 1. Go to https://aws.amazon.com → EC2 → Launch Instance
# 2. Settings:
#    Name:           artha-ai
#    AMI:            Ubuntu Server 22.04 LTS (Free tier eligible)
#    Instance type:  t2.micro (Free tier — 750 hrs/month free)
#    Key pair:       Create new → name it "artha-key" → download .pem file
#    Storage:        8 GB gp2 (free tier)
#
# 3. Security Group — Add these inbound rules:
#    Type        Port    Source
#    SSH         22      My IP (your home IP only)
#    HTTP        80      Anywhere (0.0.0.0/0)
#    HTTPS       443     Anywhere (0.0.0.0/0)
#
# 4. Launch → note your EC2 Public IPv4 address (e.g. 13.233.45.67)

# ═══════════════════════════════════════════════════════════
# PART 2 — CONNECT TO YOUR EC2
# ═══════════════════════════════════════════════════════════

# On Windows — use PowerShell or Git Bash:
chmod 400 artha-key.pem
ssh -i artha-key.pem ubuntu@YOUR_EC2_PUBLIC_IP

# On Mac/Linux:
chmod 400 ~/Downloads/artha-key.pem
ssh -i ~/Downloads/artha-key.pem ubuntu@YOUR_EC2_PUBLIC_IP

# ═══════════════════════════════════════════════════════════
# PART 3 — SERVER SETUP (run on EC2)
# ═══════════════════════════════════════════════════════════

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python, pip, nginx, git
sudo apt install -y python3 python3-pip python3-venv nginx git

# Create app directory
mkdir -p /home/ubuntu/artha_ai
cd /home/ubuntu/artha_ai

# ═══════════════════════════════════════════════════════════
# PART 4 — UPLOAD YOUR FILES (run on YOUR PC, not EC2)
# ═══════════════════════════════════════════════════════════

# Open a NEW terminal on your PC and run:
cd D:\Pro\artha_ai_trading\myenv\artha_ai

scp -i artha-key.pem app.py ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
scp -i artha-key.pem index.html ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
scp -i artha-key.pem login.html ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
scp -i artha-key.pem requirements.txt ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/

# If you have an existing database to transfer:
# scp -i artha-key.pem artha_trading.db ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/

# ═══════════════════════════════════════════════════════════
# PART 5 — INSTALL DEPENDENCIES (back on EC2)
# ═══════════════════════════════════════════════════════════

cd /home/ubuntu/artha_ai

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install all packages
pip install --upgrade pip
pip install -r requirements.txt

# Test that the app starts (Ctrl+C to stop after checking)
uvicorn app:app --host 0.0.0.0 --port 8000
# You should see: "Uvicorn running on http://0.0.0.0:8000"
# Press Ctrl+C

# ═══════════════════════════════════════════════════════════
# PART 6 — CONFIGURE SYSTEMD SERVICE
# ═══════════════════════════════════════════════════════════

# Generate a strong JWT secret (run this and copy the output)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Create the service file
sudo nano /etc/systemd/system/artha.service

# Paste this content (replace the values in CAPS):
# ─────────────────────────────────────────
# [Unit]
# Description=ARTHA AI Trading System
# After=network.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/artha_ai
# Environment="GEMINI_API_KEY=YOUR_GEMINI_API_KEY"
# Environment="JWT_SECRET=YOUR_GENERATED_SECRET_FROM_ABOVE"
# Environment="UPSTOX_REDIRECT_URI=http://YOUR_EC2_PUBLIC_IP/upstox/callback"
# ExecStart=/home/ubuntu/artha_ai/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
# Restart=always
# RestartSec=5
# StandardOutput=append:/var/log/artha_ai.log
# StandardError=append:/var/log/artha_ai_error.log
#
# [Install]
# WantedBy=multi-user.target
# ─────────────────────────────────────────
# Save: Ctrl+O → Enter → Ctrl+X

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable artha
sudo systemctl start artha

# Check it's running
sudo systemctl status artha
# Should show: Active: active (running)

# View live logs
sudo journalctl -u artha -f

# ═══════════════════════════════════════════════════════════
# PART 7 — CONFIGURE NGINX
# ═══════════════════════════════════════════════════════════

# Create nginx config
sudo nano /etc/nginx/sites-available/artha

# Paste this content (replace YOUR_EC2_PUBLIC_IP):
# ─────────────────────────────────────────
# server {
#     listen 80;
#     server_name YOUR_EC2_PUBLIC_IP;
#
#     add_header X-Frame-Options "SAMEORIGIN";
#     add_header X-Content-Type-Options "nosniff";
#
#     location / {
#         proxy_pass         http://127.0.0.1:8000;
#         proxy_http_version 1.1;
#         proxy_set_header   Upgrade $http_upgrade;
#         proxy_set_header   Connection "upgrade";
#         proxy_set_header   Host $host;
#         proxy_set_header   X-Real-IP $remote_addr;
#         proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
#         proxy_read_timeout 300s;
#     }
# }
# ─────────────────────────────────────────
# Save: Ctrl+O → Enter → Ctrl+X

# Enable the site
sudo ln -s /etc/nginx/sites-available/artha /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test nginx config
sudo nginx -t
# Should say: syntax is ok / test is successful

# Restart nginx
sudo systemctl restart nginx
sudo systemctl enable nginx

# ═══════════════════════════════════════════════════════════
# PART 8 — UPDATE UPSTOX REDIRECT URI
# ═══════════════════════════════════════════════════════════

# IMPORTANT: Update in two places:

# 1. In app.py — change UPSTOX_REDIRECT_URI at the top:
#    UPSTOX_REDIRECT_URI = "http://YOUR_EC2_PUBLIC_IP/upstox/callback"
#
#    Then re-upload app.py and restart service:
#    scp -i artha-key.pem app.py ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
#    ssh -i artha-key.pem ubuntu@YOUR_EC2_PUBLIC_IP "sudo systemctl restart artha"

# 2. In Upstox Developer Portal:
#    Go to https://account.upstox.com/developer/apps
#    Edit your app → Redirect URL → set to:
#    http://YOUR_EC2_PUBLIC_IP/upstox/callback

# ═══════════════════════════════════════════════════════════
# PART 9 — ACCESS YOUR APP
# ═══════════════════════════════════════════════════════════

# Open in browser:
# http://YOUR_EC2_PUBLIC_IP
#
# You'll see the ARTHA AI login page.
# Click "Create Account" to register your admin account first.
# Other users can register too at the same URL.

# ═══════════════════════════════════════════════════════════
# PART 10 — USEFUL COMMANDS (for day-to-day management)
# ═══════════════════════════════════════════════════════════

# Check app status
sudo systemctl status artha

# Restart app (after updating files)
sudo systemctl restart artha

# View app logs
sudo tail -f /var/log/artha_ai.log
sudo tail -f /var/log/artha_ai_error.log

# View nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log

# Update app files from your PC:
scp -i artha-key.pem app.py ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
scp -i artha-key.pem index.html ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
scp -i artha-key.pem login.html ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/
# Then restart:
ssh -i artha-key.pem ubuntu@YOUR_EC2_PUBLIC_IP "sudo systemctl restart artha"

# Backup database from server to your PC:
scp -i artha-key.pem ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/artha_ai/artha_trading.db ./artha_trading_backup.db

# Check disk space (free tier has 8GB)
df -h

# Check memory usage
free -h

# ═══════════════════════════════════════════════════════════
# PART 11 — OPTIONAL: ADD A FREE DOMAIN + HTTPS
# ═══════════════════════════════════════════════════════════

# Option A — Free domain from Freenom (freenom.com) or get one from
#            Namecheap (~$1/year for .xyz domains)
#
# Option B — Use Cloudflare Tunnel (no domain needed, free)
#   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
#   chmod +x cloudflared
#   ./cloudflared tunnel --url http://localhost:80

# If you have a domain — add HTTPS with Let's Encrypt (free):
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d yourdomain.com
# Certbot auto-renews every 90 days

# ═══════════════════════════════════════════════════════════
# PART 12 — FREE TIER LIMITS REMINDER
# ═══════════════════════════════════════════════════════════

# AWS Free Tier includes (12 months from sign-up):
# - EC2 t2.micro:  750 hours/month  (enough for 1 instance running 24/7)
# - EBS Storage:   30 GB/month
# - Data transfer: 15 GB/month outbound
# - Elastic IP:    1 free if attached to running instance
#
# COST AFTER FREE TIER:
# - t2.micro:  ~$8.50/month (Mumbai ap-south-1 region)
# - Storage:   ~$0.80/month (8GB)
# - Total:     ~$9-10/month
#
# TIP: Set up AWS Billing Alerts at $1 threshold so you never get
# surprised. Go to: AWS Console → Billing → Budgets → Create Budget

# ═══════════════════════════════════════════════════════════
# QUICK REFERENCE — CHECKLIST
# ═══════════════════════════════════════════════════════════

# [ ] 1. EC2 instance launched (t2.micro, Ubuntu 22.04)
# [ ] 2. Security group: ports 22, 80, 443 open
# [ ] 3. Files uploaded via scp
# [ ] 4. venv created, pip install -r requirements.txt done
# [ ] 5. /etc/systemd/system/artha.service created with correct env vars
# [ ] 6. sudo systemctl start artha → status shows "active (running)"
# [ ] 7. nginx installed and configured
# [ ] 8. sudo nginx -t shows "ok"
# [ ] 9. UPSTOX_REDIRECT_URI updated in app.py + Upstox Developer Portal
# [ ] 10. http://YOUR_EC2_IP opens ARTHA login page
# [ ] 11. First admin account created via register form
# [ ] 12. API keys saved, Sync Portfolio working