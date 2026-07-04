#!/usr/bin/env bash
# One-time setup on carbon. Run locally:
#   ./setup-carbon.sh
set -euo pipefail

CARBON="sokolmask@192.168.1.124"
SKILL_DIR="/home/sokolmask/hermes-data/skills/podcast"
PODCAST_DIR="/home/sokolmask/podcast-data"
REPO="https://github.com/sokoloff06/whntpdcst.git"   # update if different

echo "=== One-time setup: whntpdcst on carbon ==="

ssh "${CARBON}" bash <<REMOTE
set -e

# 1. Create dirs
mkdir -p "${PODCAST_DIR}/episodes"

# 2. Clone repo into skill dir
if [ -d "${SKILL_DIR}/.git" ]; then
  echo "Repo already cloned — pulling"
  cd "${SKILL_DIR}" && git pull
else
  git clone "${REPO}" "${SKILL_DIR}"
fi

# 3. Install Python deps
/opt/hermes/.venv/bin/pip install -q -r "${SKILL_DIR}/requirements.txt"

# 4. Copy docker config and start static server
cp "${SKILL_DIR}/docker/nginx.conf"        "${PODCAST_DIR}/nginx-podcast-location.conf"
cp "${SKILL_DIR}/docker/docker-compose.yml" "${PODCAST_DIR}/docker-compose.yml"

cd "${PODCAST_DIR}"
docker compose up -d

# 5. Install cloudflared (for Cloudflare Tunnel)
if ! command -v cloudflared &>/dev/null; then
  echo "Installing cloudflared..."
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list
  sudo apt-get update -qq && sudo apt-get install -y cloudflared
  echo "cloudflared installed. Next: cloudflared tunnel login && cloudflared tunnel create whntpdcst"
else
  echo "cloudflared already installed: \$(cloudflared --version)"
fi

echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "1. Add .env with keys: cp ${SKILL_DIR}/.env.example /home/sokolmask/hermes-data/.env"
echo "   (or add to existing Hermes .env)"
echo ""
echo "2. Cloudflare Tunnel:"
echo "   cloudflared tunnel login"
echo "   cloudflared tunnel create whntpdcst"
echo "   cloudflared tunnel route dns whntpdcst whntpdcst.com"
echo "   # Add to /etc/cloudflared/config.yml:"
echo "   #   tunnel: <TUNNEL_ID>"
echo "   #   ingress:"
echo "   #     - hostname: whntpdcst.com"
echo "   #       service: http://localhost:8085"
echo "   #     - service: http_status:404"
echo "   cloudflared service install && systemctl start cloudflared"
echo ""
echo "3. GitHub Secrets (repo settings):"
echo "   CARBON_HOST = 192.168.1.124  (or LAN hostname)"
echo "   CARBON_SSH_KEY = <private key content>"
echo ""
echo "4. Test podcast:"
echo "   ssh ${CARBON}"
echo "   cd ${SKILL_DIR}"
echo "   YOUTUBE_API_KEY=... OPENROUTER_API_KEY=... /opt/hermes/.venv/bin/python podcast_skill.py --dry-run"
REMOTE
