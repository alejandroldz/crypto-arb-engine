# Despliegue 24/7 en Oracle Cloud Always Free

El engine corre en una VM ARM gratuita para siempre (Oracle Cloud Always Free),
con tests + despliegue automático en cada push a `main` vía GitHub Actions.

## Lo que da Oracle gratis (verificado 2026-11-13, doc oficial)

| Recurso | Always Free |
|---|---|
| VM.Standard.A1.Flex (ARM Ampere) | 2 OCPU + 12 GB RAM en total (1 o 2 VMs) |
| Disco (boot + block volumes) | 200 GB en total (mínimo 47-50 GB por VM) |
| Salida de red | 10 TB/mes |
| IP pública | Incluida |
| VMs AMD E2.1.Micro | 2 adicionales (1 GB RAM cada una) |

Letra pequeña:
- **El alta pide tarjeta** solo para verificar identidad. No cobra nada si solo
  usas recursos "Always Free" (los marca con esa etiqueta en la consola).
- **Regla de inactividad**: si durante 7 días la VM va a <20% de CPU (p95),
  <20% de red y <20% de memoria, Oracle la puede reclamar (la apaga; la
  reinicias y sigue). El engine con websockets 24/7 y ~1-2 GB de RAM sobre 12 GB
  debería quedar fuera de "idle" por red y memoria, pero si Oracle avisa, se
  reinicia y listo.
- **Capacidad ARM**: en regiones populares a veces dice "out of capacity for
  shape VM.Standard.A1.Flex". Se resuelve reintentando en otro Availability
  Domain o a otra hora (la capacidad se libera a lo largo del día).
- La **región home** se elige al crear la cuenta y no se puede cambiar; los
  recursos gratis solo viven ahí. Elige una cercana (Madrid existe: `eu-madrid-1`).
- Muchas guías dicen "4 OCPU / 24 GB": eso aplica a cuentas de pago. Una cuenta
  Always Free pura tiene **2 OCPU / 12 GB** (sobra y basta para el engine).

## Pasos (los hace el dueño de la cuenta)

### 1. Crear la cuenta Oracle
1. https://www.oracle.com/cloud/free/ → Sign up. Región home: **Madrid**.
2. Tarjeta solo para verificación de identidad (cargo temporal de ~1€ que se revierte).

### 2. Crear la VM
1. Console → Compute → Instances → **Create instance**.
2. Name: `crypto-arb`. Image: **Ubuntu 22.04 (aarch64)**.
3. Shape: **VM.Standard.A1.Flex** → 2 OCPU, 12 GB RAM.
4. Networking: deja la VCN/subnet por defecto con IP pública.
5. SSH keys: **Generate a key pair for me** → descarga la **private key**
   (la necesitas en el paso 5).
6. Boot volume: 50 GB (por defecto) sobra.
7. Create. Si dice "out of capacity": cambia de Availability Domain o reintenta luego.

### 3. Preparar la VM (una vez, ~5 min)
Conéctate: `ssh ubuntu@<IP-publica>` con la clave descargada. Luego:

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit   # vuelve a entrar para que coja el grupo docker

# Repo
sudo mkdir -p /opt/crypto-arb
sudo chown ubuntu:ubuntu /opt/crypto-arb
git clone https://github.com/alejandroldz/crypto-arb-engine /opt/crypto-arb
cd /opt/crypto-arb
cp .env.example .env
nano .env     # pega las API keys reales (mismas que usa el soak)
mkdir -p data

# Primer arranque manual
docker compose up -d --build
docker compose logs -f   # Ctrl+C para salir; el engine sigue corriendo
```

### 4. Activar el despliegue automático en GitHub
En el repo → **Settings → Secrets and variables → Actions**:

**Secrets** (pestaña Secrets):
| Nombre | Valor |
|---|---|
| `VM_HOST` | IP pública de la VM |
| `VM_USER` | `ubuntu` |
| `VM_SSH_KEY` | contenido completo de la private key descargada |

**Variables** (pestaña Variables):
| Nombre | Valor |
|---|---|
| `DEPLOY_ENABLED` | `true` |

### 5. Listo
Cada push a `main`: corre la suite; si está verde, la VM hace
`git pull && docker compose up -d --build` sola. Si los tests fallan, no se
despliega nada.

## Operación diaria
- Ver logs: `ssh ubuntu@<IP> "cd /opt/crypto-arb && docker compose logs -f"`
- Estado: `docker compose ps` (incluye health check).
- Los datos (SQLite/DuckDB) viven en `/opt/crypto-arb/data` en la VM, fuera del
  contenedor: redesplegar no borra nada.
- Telegram sigue llegando igual (el .env de la VM lleva el mismo token/chat).
