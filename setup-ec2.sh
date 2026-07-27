#!/bin/bash
# ==============================================================================
# Script de despliegue para AWS EC2 (diseñado para Ubuntu/Debian/Amazon Linux)
# ==============================================================================
set -e

echo "=== [1/4] Actualizando el sistema e instalando dependencias básicas ==="
if [ -f /etc/debian_version ]; then
    sudo apt-get update -y
    sudo apt-get install -y curl git
elif [ -f /etc/redhat-release ] || [ -f /etc/system-release ]; then
    sudo yum update -y
    sudo yum install -y curl git
else
    echo "Distribución no soportada automáticamente para actualización de paquetes. Saltando al instalador de Docker..."
fi

echo "=== [2/4] Instalando Docker y Docker Compose (script oficial) ==="
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    rm get-docker.sh
else
    echo "Docker ya está instalado."
fi

# Habilitar e iniciar Docker
sudo systemctl enable docker || true
sudo systemctl start docker || true

# Agregar el usuario actual al grupo docker
sudo usermod -aG docker $USER || true

echo "=== [3/4] Levantando el contenedor con Docker Compose ==="
# Usamos sudo por si el grupo docker no se ha refrescado en la sesión actual
sudo docker compose up -d --build

echo "=== [4/4] Verificando el estado del servicio ==="
sudo docker compose ps

echo "=============================================================================="
echo "¡Despliegue inicial completado!"
echo "El servicio debería estar corriendo en el puerto 8000."
echo "Para verificar el estado en tiempo real, corre: sudo docker compose logs -f"
echo "Nota: Recuerda abrir el puerto 8000 en el Security Group de tu EC2."
echo "=============================================================================="
