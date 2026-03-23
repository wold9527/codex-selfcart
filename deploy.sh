#!/bin/bash
# ========================================
# 自动部署脚本 - 适用于 Ubuntu/Debian 小鸡
# 1H1G 优化版
# ========================================
set -e

APP_DIR="/opt/auto_bindcard"
APP_USER="bindcard"
STREAMLIT_PORT=8501

echo "============================="
echo "  自动绑卡工具 - 部署脚本"
echo "============================="

# 检查 root
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请使用 root 运行: sudo bash deploy.sh"
    exit 1
fi

# ---- 1. 系统依赖 ----
echo ""
echo "[1/7] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    xvfb \
    wget curl unzip \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libcups2 \
    libxkbcommon0 libgtk-3-0 fonts-liberation \
    > /dev/null 2>&1
echo "  ✅ 系统依赖已安装"

# ---- 2. Swap (1H1G 必需) ----
echo ""
echo "[2/7] 配置 Swap..."
if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile > /dev/null
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "  ✅ 已创建 1G Swap"
else
    echo "  ⏭ Swap 已存在"
fi

# ---- 3. 创建用户和目录 ----
echo ""
echo "[3/7] 创建应用用户和目录..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$APP_USER"
    echo "  ✅ 已创建用户 $APP_USER"
else
    echo "  ⏭ 用户 $APP_USER 已存在"
fi

mkdir -p "$APP_DIR"
cp -r ./* "$APP_DIR/"
cp -r .streamlit "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
echo "  ✅ 文件已复制到 $APP_DIR"

# ---- 4. Python 虚拟环境 ----
echo ""
echo "[4/7] 创建 Python 虚拟环境..."
cd "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv venv
sudo -u "$APP_USER" venv/bin/pip install --upgrade pip -q
sudo -u "$APP_USER" venv/bin/pip install -r requirements.txt -q
sudo -u "$APP_USER" venv/bin/pip install playwright -q
echo "  ✅ Python 依赖已安装"

# ---- 5. 安装 Playwright Chromium ----
echo ""
echo "[5/7] 安装 Playwright Chromium..."
sudo -u "$APP_USER" venv/bin/playwright install chromium
echo "  ✅ Chromium 已安装"

# ---- 6. 配置文件 ----
echo ""
echo "[6/7] 检查配置文件..."
if [ ! -f "$APP_DIR/config.json" ]; then
    cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
    echo "  ⚠️  已从模板创建 config.json，请编辑: $APP_DIR/config.json"
else
    echo "  ⏭ config.json 已存在"
fi

# ---- 7. Systemd 服务 ----
echo ""
echo "[7/8] 配置 systemd 服务..."

cat > /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=Xvfb Virtual Framebuffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x720x24 -ac -nolisten tcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/auto-bindcard.service << EOF
[Unit]
Description=Auto BindCard Web UI
After=network.target xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=DISPLAY=:99
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/streamlit run ui.py \\
    --server.port=$STREAMLIT_PORT \\
    --server.address=0.0.0.0 \\
    --server.headless=true \\
    --server.maxUploadSize=5 \\
    --browser.gatherUsageStats=false
Restart=always
RestartSec=10
# 内存限制 (防止 OOM killer)
MemoryMax=800M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xvfb auto-bindcard
systemctl start xvfb
systemctl start auto-bindcard

echo "  ✅ 服务已启动"

# ---- 8. 初始化数据库 & 修复权限 ----
echo ""
echo "[8/8] 初始化数据库..."
cd "$APP_DIR"
sudo -u "$APP_USER" venv/bin/python -c "from database import init_db; init_db(); print('  ✅ 数据库已初始化')"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---- 完成 ----
echo ""
echo "============================="
echo "  ✅ 部署完成!"
echo "============================="
echo ""
echo "  📦 应用目录: $APP_DIR"
echo "  🌐 访问地址: http://$(hostname -I | awk '{print $1}'):$STREAMLIT_PORT"
echo "  📝 配置文件: $APP_DIR/config.json"
echo ""
echo "  常用命令:"
echo "    查看状态:  systemctl status auto-bindcard"
echo "    查看日志:  journalctl -u auto-bindcard -f"
echo "    重启服务:  systemctl restart auto-bindcard"
echo "    管理兑换码: cd $APP_DIR && venv/bin/python admin_cli.py generate 10 --uses 5"
echo ""
