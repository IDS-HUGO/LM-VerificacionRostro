#!/usr/bin/env bash
# ==============================================================================
# Despliegue completo del microservicio KYC (LM-VerificacionRostro) en EC2
# Ubuntu, detrás de nginx con HTTPS (Let's Encrypt) en un subdominio propio.
#
# Qué hace, en orden:
#   1. Valida argumentos/entorno (dominio, email, permisos, SO).
#   2. Actualiza el sistema e instala dependencias (con reintentos).
#   3. Instala Docker (si falta) y lo habilita.
#   4. Instala nginx + certbot.
#   5. Configura ufw (firewall): abre 22/80/443, el 8000 NUNCA se expone.
#   6. Clona o actualiza el repo (git clone / git pull, idempotente).
#   7. Escribe .env con las variables del servicio (incluye ALLOWED_ORIGINS).
#   8. Levanta el contenedor con docker compose y espera a que /health responda.
#   9. Valida que el DNS del subdominio ya apunte a esta IP pública ANTES de
#      pedir el certificado (evita el error más común de certbot).
#  10. Escribe la config de nginx como reverse proxy hacia 127.0.0.1:8000.
#  11. Obtiene certificado TLS con certbot --nginx (con reintentos) y confirma
#      que el timer de renovación automática está activo.
#  12. Prueba el endpoint público por HTTPS y muestra un resumen final.
#
# Es SEGURO volver a correrlo (idempotente): detecta lo que ya está hecho y
# lo salta o lo actualiza, no lo duplica.
#
# Uso:
#   sudo DOMAIN=kyc.tudominio.com EMAIL=tu@correo.com \
#        ALLOWED_ORIGINS="https://app.tudominio.com" ./setup-ec2.sh
#
#   o con flags:
#   sudo ./setup-ec2.sh --domain kyc.tudominio.com --email tu@correo.com \
#        --origins "https://app.tudominio.com,https://admin.tudominio.com"
#
#   Si no pasas --domain/--email, el script los pide de forma interactiva.
# ==============================================================================

set -Eeuo pipefail

# ------------------------------------------------------------------------------
# 0. Configuración por defecto / parseo de argumentos
# ------------------------------------------------------------------------------
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-}"
REPO_URL="${REPO_URL:-https://github.com/IDS-HUGO/LM-VerificacionRostro.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-$HOME/LM-VerificacionRostro}"
APP_PORT="${APP_PORT:-8000}"
SKIP_DNS_CHECK="${SKIP_DNS_CHECK:-false}"
OPEN_SG="${OPEN_SG:-false}"
NON_INTERACTIVE="${NON_INTERACTIVE:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --origins) ALLOWED_ORIGINS="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --skip-dns-check) SKIP_DNS_CHECK="true"; shift ;;
    --open-sg) OPEN_SG="true"; shift ;;
    --yes|--non-interactive) NON_INTERACTIVE="true"; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Argumento desconocido: $1"; exit 1 ;;
  esac
done

# ------------------------------------------------------------------------------
# 1. Logging + manejo de errores
# ------------------------------------------------------------------------------
LOG_FILE="/tmp/kyc-deploy-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

STEP=""
step() { STEP="$1"; echo ""; echo "=== $1 ==="; }

on_error() {
  local exit_code=$1 line_no=$2
  echo ""
  echo "❌ ERROR (código $exit_code) en la línea $line_no, durante el paso: '$STEP'"
  echo "   Log completo guardado en: $LOG_FILE"
  echo "   El script es idempotente: corrígelo y vuelve a correrlo, no duplicará pasos ya hechos."
  case "$STEP" in
    *"certbot"*|*"certificado"*)
      echo "   Sugerencia: revisa que el subdominio ya resuelva a la IP pública de esta"
      echo "   instancia (propaga el DNS) y que el puerto 80 esté abierto en el Security Group."
      ;;
    *"nginx"*)
      echo "   Sugerencia: corre 'sudo nginx -t' para ver el error de sintaxis exacto."
      ;;
    *"docker compose"*|*"contenedor"*)
      echo "   Sugerencia: corre 'sudo docker compose -f $APP_DIR/docker-compose.yml logs --tail=100' para ver el detalle."
      ;;
  esac
  exit "$exit_code"
}
trap 'on_error $? $LINENO' ERR

require_cmd() {
  command -v "$1" >/dev/null 2>&1
}

retry() {
  # retry <intentos> <segundos_entre_intentos> <comando...>
  local max=$1 delay=$2; shift 2
  local n=1
  until "$@"; do
    if [[ $n -ge $max ]]; then
      echo "El comando falló después de $max intentos: $*"
      return 1
    fi
    echo "Intento $n/$max falló, reintentando en ${delay}s..."
    n=$((n + 1))
    sleep "$delay"
  done
}

# ------------------------------------------------------------------------------
# 2. Validaciones previas
# ------------------------------------------------------------------------------
step "Validando entorno y argumentos"

if [[ "$(id -u)" -ne 0 ]]; then
  if require_cmd sudo; then
    echo "No estás como root; se usará sudo para los pasos que lo requieran."
    SUDO="sudo"
  else
    echo "Este script necesita privilegios de root o sudo. Corre como root o instala sudo."
    exit 1
  fi
else
  SUDO=""
fi

if [[ ! -f /etc/os-release ]] || ! grep -qiE 'ubuntu|debian' /etc/os-release; then
  echo "⚠️  Este script está pensado para Ubuntu/Debian. Se detectó otro SO;"
  echo "   continuará, pero los pasos de apt/ufw podrían fallar."
fi

DOMAIN_RE='^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'

if [[ -z "$DOMAIN" ]]; then
  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    echo "Falta --domain y estás en modo no interactivo."; exit 1
  fi
  read -rp "Subdominio para el servicio (ej: kyc.tudominio.com): " DOMAIN
fi
if [[ ! "$DOMAIN" =~ $DOMAIN_RE ]]; then
  echo "❌ '$DOMAIN' no parece un dominio válido."; exit 1
fi

if [[ -z "$EMAIL" ]]; then
  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    echo "Falta --email y estás en modo no interactivo."; exit 1
  fi
  read -rp "Email para avisos de Let's Encrypt (renovación/expiración): " EMAIL
fi
if [[ ! "$EMAIL" =~ $EMAIL_RE ]]; then
  echo "❌ '$EMAIL' no parece un email válido."; exit 1
fi

if [[ -z "$ALLOWED_ORIGINS" && "$NON_INTERACTIVE" != "true" ]]; then
  read -rp "Orígenes permitidos para CORS, separados por coma (Enter para dejarlo vacío = sin CORS, solo backend/Flutter consumen esto): " ALLOWED_ORIGINS || true
fi

echo "Dominio:          $DOMAIN"
echo "Email:            $EMAIL"
echo "CORS origins:     ${ALLOWED_ORIGINS:-'(ninguno; CORS deshabilitado)'}"
echo "Repo:             $REPO_URL ($BRANCH)"
echo "Directorio app:   $APP_DIR"

# ------------------------------------------------------------------------------
# 3. Sistema base
# ------------------------------------------------------------------------------
step "Actualizando el sistema e instalando dependencias base"

if require_cmd apt-get; then
  export DEBIAN_FRONTEND=noninteractive
  # Espera a que se libere el lock de apt/dpkg si otro proceso (p. ej.
  # unattended-upgrades) lo tiene tomado, hasta 5 minutos.
  wait_apt_lock() {
    local waited=0
    while $SUDO fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
      if [[ $waited -ge 300 ]]; then
        echo "El lock de apt sigue tomado después de 5 minutos, abortando."; return 1
      fi
      echo "apt/dpkg ocupado por otro proceso, esperando... (${waited}s)"
      sleep 10; waited=$((waited + 10))
    done
  }
  wait_apt_lock
  retry 3 10 $SUDO apt-get update -y
  retry 3 10 $SUDO apt-get install -y curl git ufw nginx certbot python3-certbot-nginx ca-certificates gnupg dnsutils
elif require_cmd yum; then
  retry 3 10 $SUDO yum update -y
  retry 3 10 $SUDO yum install -y curl git nginx certbot python3-certbot-nginx bind-utils
  $SUDO systemctl enable --now firewalld || true
else
  echo "❌ No se encontró apt-get ni yum. No se puede continuar automáticamente."
  exit 1
fi

# ------------------------------------------------------------------------------
# 4. Docker
# ------------------------------------------------------------------------------
step "Instalando Docker (si falta)"

if ! require_cmd docker; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  retry 2 5 $SUDO sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
else
  echo "Docker ya está instalado ($(docker --version))."
fi

$SUDO systemctl enable docker
$SUDO systemctl start docker

if ! docker compose version >/dev/null 2>&1; then
  echo "❌ 'docker compose' (plugin v2) no está disponible tras instalar Docker."
  echo "   Revisa https://docs.docker.com/compose/install/ manualmente."
  exit 1
fi

CURRENT_USER="${SUDO_USER:-$USER}"
if [[ -n "$CURRENT_USER" && "$CURRENT_USER" != "root" ]]; then
  $SUDO usermod -aG docker "$CURRENT_USER" || true
fi

# ------------------------------------------------------------------------------
# 5. Firewall (ufw)
# ------------------------------------------------------------------------------
step "Configurando firewall (ufw)"

if require_cmd ufw; then
  $SUDO ufw allow OpenSSH || $SUDO ufw allow 22/tcp
  $SUDO ufw allow 'Nginx Full' || { $SUDO ufw allow 80/tcp; $SUDO ufw allow 443/tcp; }
  # Defensa en profundidad: el puerto de la app solo debe verse en localhost
  # (docker-compose.yml ya lo publica en 127.0.0.1), pero negamos explícito
  # cualquier acceso externo por si algún día cambia el compose.
  $SUDO ufw deny "$APP_PORT"/tcp || true
  if ! $SUDO ufw status | grep -q "Status: active"; then
    $SUDO ufw --force enable
  fi
  $SUDO ufw status verbose || true
else
  echo "⚠️  ufw no disponible; asegúrate de cerrar el puerto $APP_PORT en el firewall/Security Group manualmente."
fi

# (Opcional) intentar abrir 80/443 en el Security Group de AWS si hay aws-cli
# configurado. Best-effort: si falla, NO detiene el despliegue.
if [[ "$OPEN_SG" == "true" ]]; then
  step "Intentando abrir 80/443 en el Security Group de AWS (best-effort)"
  if require_cmd aws && require_cmd curl; then
    set +e
    TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" --max-time 3)
    INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id --max-time 3)
    REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region --max-time 3)
    if [[ -n "$INSTANCE_ID" && -n "$REGION" ]]; then
      SG_ID=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$REGION" \
        --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text 2>/dev/null)
      if [[ -n "$SG_ID" && "$SG_ID" != "None" ]]; then
        aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" --protocol tcp --port 80 --cidr 0.0.0.0/0 2>/dev/null
        aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" --protocol tcp --port 443 --cidr 0.0.0.0/0 2>/dev/null
        echo "Reglas 80/443 solicitadas en el Security Group $SG_ID (si ya existían, se ignoró el error)."
      fi
    fi
    set -e
  else
    echo "aws-cli no disponible/configurado; omitiendo. Abre 80 y 443 manualmente en el Security Group."
  fi
fi

# ------------------------------------------------------------------------------
# 6. Clonar / actualizar repo
# ------------------------------------------------------------------------------
step "Clonando o actualizando el repositorio"

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  retry 3 5 git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# ------------------------------------------------------------------------------
# 7. Variables de entorno del servicio
# ------------------------------------------------------------------------------
step "Escribiendo .env"

cat > "$APP_DIR/.env" <<EOF
APP_PORT=${APP_PORT}
SIMILARITY_THRESHOLD=${SIMILARITY_THRESHOLD:-0.32}
DETECTOR_BACKEND=${DETECTOR_BACKEND:-retinaface}
MODEL_NAME=${MODEL_NAME:-ArcFace}
MAX_UPLOAD_SIZE_MB=${MAX_UPLOAD_SIZE_MB:-8}
ALLOWED_ORIGINS=${ALLOWED_ORIGINS}
EOF
echo ".env escrito en $APP_DIR/.env"

# ------------------------------------------------------------------------------
# 8. Levantar el contenedor
# ------------------------------------------------------------------------------
step "Construyendo y levantando el contenedor con docker compose"

retry 2 10 $SUDO docker compose -f "$APP_DIR/docker-compose.yml" --env-file "$APP_DIR/.env" up -d --build

echo "Esperando a que el servicio responda en http://127.0.0.1:${APP_PORT}/health ..."
HEALTH_OK="false"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
    HEALTH_OK="true"
    break
  fi
  echo "  intento $i/30, esperando 5s... (la primera vez puede tardar por la descarga de pesos de DeepFace)"
  sleep 5
done

if [[ "$HEALTH_OK" != "true" ]]; then
  echo "❌ El contenedor no respondió en /health después de 150s."
  echo "   Logs recientes:"
  $SUDO docker compose -f "$APP_DIR/docker-compose.yml" logs --tail=80
  exit 1
fi
echo "✅ Contenedor arriba y saludable."

# ------------------------------------------------------------------------------
# 9. Validar DNS antes de pedir el certificado
# ------------------------------------------------------------------------------
step "Validando que el DNS de $DOMAIN apunte a esta instancia"

if [[ "$SKIP_DNS_CHECK" != "true" ]]; then
  PUBLIC_IP="$(curl -fsS --max-time 5 https://checkip.amazonaws.com || true)"
  PUBLIC_IP="$(echo "$PUBLIC_IP" | tr -d '[:space:]')"
  DOMAIN_IP="$(dig +short "$DOMAIN" A | tail -n1 || true)"

  if [[ -z "$PUBLIC_IP" ]]; then
    echo "⚠️  No se pudo determinar la IP pública de esta instancia; se omite la validación de DNS."
  elif [[ -z "$DOMAIN_IP" ]]; then
    echo "❌ '$DOMAIN' todavía no resuelve a ninguna IP. Antes de pedir el certificado TLS,"
    echo "   crea un registro A en tu DNS: $DOMAIN -> $PUBLIC_IP, y espera a que propague"
    echo "   (puede tardar desde minutos hasta un par de horas)."
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
      echo "   Modo no interactivo: abortando antes de certbot. Vuelve a correr el script cuando el DNS ya resuelva."
      exit 1
    fi
    read -rp "¿Ya configuraste el DNS y quieres reintentar la validación ahora? (s/N) " ans
    if [[ "$ans" =~ ^[sS]$ ]]; then
      DOMAIN_IP="$(dig +short "$DOMAIN" A | tail -n1 || true)"
    fi
    if [[ -z "$DOMAIN_IP" ]]; then
      echo "Sigue sin resolver. Corre el script de nuevo cuando el DNS esté propagado."
      exit 1
    fi
  fi

  if [[ -n "$DOMAIN_IP" && -n "$PUBLIC_IP" && "$DOMAIN_IP" != "$PUBLIC_IP" ]]; then
    echo "⚠️  '$DOMAIN' resuelve a $DOMAIN_IP, pero la IP pública de esta instancia es $PUBLIC_IP."
    echo "   certbot probablemente fallará el reto HTTP-01 si esto no coincide."
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
      exit 1
    fi
    read -rp "¿Continuar de todas formas? (s/N) " ans
    [[ "$ans" =~ ^[sS]$ ]] || exit 1
  else
    echo "✅ DNS OK: $DOMAIN -> $DOMAIN_IP"
  fi
else
  echo "Validación de DNS omitida (--skip-dns-check)."
fi

# ------------------------------------------------------------------------------
# 10. Configurar nginx como reverse proxy
# ------------------------------------------------------------------------------
step "Configurando nginx (reverse proxy)"

NGINX_SITE="/etc/nginx/sites-available/${DOMAIN}"
$SUDO tee "$NGINX_SITE" > /dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    # Límite de subida: dos archivos (id_document + selfie) con margen sobre
    # MAX_UPLOAD_SIZE_MB (default 8MB c/u).
    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # DeepFace/RetinaFace pueden tardar, sobre todo la primera vez que
        # descargan pesos del modelo; damos margen generoso.
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }
}
EOF

$SUDO ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/${DOMAIN}"
# Quita el sitio default si sigue habilitado, para que no compita en el puerto 80.
[[ -f /etc/nginx/sites-enabled/default ]] && $SUDO rm -f /etc/nginx/sites-enabled/default

$SUDO nginx -t
$SUDO systemctl reload nginx || $SUDO systemctl restart nginx
echo "✅ nginx configurado y recargado."

# ------------------------------------------------------------------------------
# 11. Certificado TLS con certbot
# ------------------------------------------------------------------------------
step "Obteniendo/renovando certificado TLS con certbot"

if $SUDO certbot certificates 2>/dev/null | grep -q "Domains: .*\b${DOMAIN}\b"; then
  echo "Ya existe un certificado para $DOMAIN; certbot renovará solo si hace falta."
  retry 2 10 $SUDO certbot renew --nginx --cert-name "$DOMAIN" --non-interactive || true
else
  retry 2 15 $SUDO certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
fi

$SUDO nginx -t
$SUDO systemctl reload nginx

if $SUDO systemctl is-enabled --quiet certbot.timer 2>/dev/null || $SUDO systemctl is-active --quiet snap.certbot.renew.timer 2>/dev/null; then
  echo "✅ Renovación automática de certbot está activa."
else
  echo "⚠️  No se detectó el timer de renovación automática de certbot activo."
  echo "   Verifica con: systemctl list-timers | grep certbot"
fi

# ------------------------------------------------------------------------------
# 12. Verificación final
# ------------------------------------------------------------------------------
step "Verificación final"

FINAL_OK="false"
for i in $(seq 1 10); do
  if curl -fsS "https://${DOMAIN}/health" >/dev/null 2>&1; then
    FINAL_OK="true"
    break
  fi
  sleep 3
done

echo ""
echo "=============================================================================="
if [[ "$FINAL_OK" == "true" ]]; then
  echo "🎉 Despliegue completo y verificado."
  echo "   Endpoint público:  https://${DOMAIN}/health"
  echo "   Endpoint de verificación: https://${DOMAIN}/verify (POST multipart: id_document, selfie)"
else
  echo "⚠️  El despliegue terminó pero https://${DOMAIN}/health no respondió 200 todavía."
  echo "   Puede ser solo propagación de DNS/cert. Prueba en unos minutos:"
  echo "   curl -v https://${DOMAIN}/health"
fi
echo ""
echo "CORS:              ${ALLOWED_ORIGINS:-'(deshabilitado; solo backend/Flutter consumen la API)'}"
echo "Ver logs del app:  sudo docker compose -f ${APP_DIR}/docker-compose.yml logs -f"
echo "Ver logs de nginx: sudo tail -f /var/log/nginx/error.log"
echo "Redesplegar:       vuelve a correr este mismo script (es idempotente)"
echo "Log de esta corrida: $LOG_FILE"
echo "=============================================================================="