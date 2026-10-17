#!/usr/bin/env bash
# Despliegue del Gestor de Tareas por entornos (dev y pro) en un servidor
# con Docker. Ver docs/DESPLIEGUE.md.
#
#   tareas.sh preparar IMAGEN          primera vez: carpetas, secretos, MariaDB
#                                      compartida y clave SSH para GitHub
#   tareas.sh desplegar ENT VERSION    pone esa version (v1.2.0, sha-1a2b3c4...)
#   tareas.sh rollback ENT [VERSION]   vuelve a la version buena anterior
#   tareas.sh estado                   que version tiene cada entorno
#   tareas.sh historial ENT            versiones desplegadas en ENT
#   tareas.sh copia ENT                copia de la base de datos de ENT
#   tareas.sh restaurar ENT FICHERO    vuelve la BD de ENT a una copia
#   tareas.sh pro-a-dev                copia los datos de pro en dev
#   tareas.sh logs ENT                 log de la app de ENT
#   tareas.sh keycloak ENT URL ISSUER [CLIENTE]
#                                      login unico con Keycloak en ENT (pide
#                                      el client secret y vuelve a arrancar)
#   tareas.sh importar ENT ORIGEN [APP]  trae los datos (y adjuntos) de una
#                                      instalacion de antes (contenedor o volcado)
#
# La ruta de cada entorno (pro.<dominio>/tareas/...), la portada, los
# dominios propios y las demas apps no son cosa de este script: los lleva el
# servidor (su entrada lee servidor.json) o el proxy que tengas delante, que
# solo tiene que mandar /tareas/ al contenedor tareas-<ENT>:8000 con la
# cabecera X-Forwarded-Prefix: /tareas.
#
# ENT = dev | pro. Todo vive junto a este script (p. ej. /opt/apps/tareas):
#   <ENT>/.env                secretos y version actual (lo crea "preparar")
#   <ENT>/app.env             marca, modulos y login de ese entorno
#   <ENT>/docker-compose.yml  sacado de la imagen de la version desplegada
#   <ENT>/historial.log       que se desplego y cuando
#   bd/                       MariaDB compartida (contenedor tareas-bd)
#   copias/<ENT>/             copias de la BD (antes de cada despliegue en pro)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTORNOS="dev pro"
ESPERA_SALUD=${ESPERA_SALUD:-300}   # segundos para que la version nueva este sana
COPIAS_A_GUARDAR=${COPIAS_A_GUARDAR:-15}
VERSIONES_A_GUARDAR=${VERSIONES_A_GUARDAR:-4}

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "· $*"; }
ahora() { date -u +%Y-%m-%dT%H:%M:%SZ; }

entorno_valido() {
  case " $ENTORNOS " in *" $1 "*) ;; *) die "entorno '$1' no válido (dev o pro)";; esac
  [[ -f "$DIR/$1/.env" ]] || die "el entorno '$1' no está preparado (tareas.sh preparar)"
}

version_valida() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "versión no válida: '$1'"
}

# Lee una variable de un fichero .env sin ejecutarlo
var() { grep -E "^$2=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

poner_var() {   # poner_var FICHERO CLAVE VALOR
  if grep -qE "^$2=" "$1"; then
    sed -i "s|^$2=.*|$2=$3|" "$1"
  else
    echo "$2=$3" >> "$1"
  fi
}

secreto() { openssl rand -hex 24; }

dc() {   # docker compose de un entorno
  local ent=$1; shift
  docker compose --project-directory "$DIR/$ent" -p "tareas-$ent" -f "$DIR/$ent/docker-compose.yml" "$@"
}

bd_root() { var "$DIR/bd/.env" MARIADB_ROOT_PASSWORD; }

bd_sql() {   # ejecuta SQL (por stdin) como root en la MariaDB compartida
  docker exec -i -e MYSQL_PWD="$(bd_root)" tareas-bd mariadb -uroot "$@"
}

# ── MariaDB compartida ───────────────────────────────────────────────────────
escribir_compose_bd() {
  mkdir -p "$DIR/bd"
  cat > "$DIR/bd/docker-compose.yml" <<'YAML'
# MariaDB compartida por los entornos dev y pro del Gestor de Tareas.
# Una sola instancia (el servidor tiene poca memoria); cada entorno tiene su
# usuario y solo ve sus bases de datos. Sin puertos publicados: solo la red
# interna tareas_bd. Lo escribe "tareas.sh preparar".
name: tareas-bd
services:
  bd:
    image: mariadb:11.4
    container_name: tareas-bd
    restart: unless-stopped
    environment:
      MARIADB_ROOT_PASSWORD: ${MARIADB_ROOT_PASSWORD:?falta MARIADB_ROOT_PASSWORD}
    command:
      - --innodb-buffer-pool-size=${BD_BUFFER:-384M}
      - --max-connections=80
      - --performance-schema=OFF
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
    mem_limit: ${BD_MEMORIA:-1g}
    volumes:
      - datos:/var/lib/mysql
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 30
    networks:
      - bd
volumes:
  datos:
networks:
  bd:
    name: tareas_bd
YAML
}

esperar_bd() {
  local i
  for i in $(seq 1 60); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' tareas-bd 2>/dev/null)" == healthy ]] && return 0
    sleep 3
  done
  die "MariaDB (tareas-bd) no arranca: docker logs tareas-bd"
}

# ── preparar ─────────────────────────────────────────────────────────────────
cmd_preparar() {
  local imagen=${1:-}
  [[ -n "$imagen" ]] || die "uso: tareas.sh preparar IMAGEN   (p. ej. ghcr.io/usuario/app_tareas_base)"
  imagen=${imagen,,}
  command -v docker >/dev/null || die "falta Docker"
  command -v openssl >/dev/null || die "falta openssl"
  local red_proxy=${RED_PROXY:-proxy_default}
  docker network inspect "$red_proxy" >/dev/null 2>&1 \
    || die "no existe la red Docker '$red_proxy' del proxy (pon RED_PROXY=<red> delante si se llama de otra forma)"
  umask 077

  # MariaDB compartida
  escribir_compose_bd
  [[ -f "$DIR/bd/.env" ]] || echo "MARIADB_ROOT_PASSWORD=$(secreto)" > "$DIR/bd/.env"
  docker compose -f "$DIR/bd/docker-compose.yml" up -d
  log "esperando a MariaDB..."
  esperar_bd

  local ent demo memoria nombre
  for ent in $ENTORNOS; do
    mkdir -p "$DIR/$ent" "$DIR/copias/$ent"
    case $ent in
      dev) demo=1; memoria=512m; nombre="Gestor de Tareas (DEV)";;
      pro) demo=0; memoria=768m; nombre="Gestor de Tareas";;
    esac
    if [[ ! -f "$DIR/$ent/.env" ]]; then
      cat > "$DIR/$ent/.env" <<EOF
# Entorno $ent -- lo crea tareas.sh preparar. No lo copies fuera del servidor.
ENTORNO=$ent
IMAGEN=$imagen
VERSION=
RED_PROXY=$red_proxy
APP_MEMORIA=$memoria
DB_USER=tareas_$ent
DB_PASSWORD=$(secreto)
DB_NAME=app_tareas_$ent
DB_NAME_TEST=app_tareas_${ent}_test
INSTALAR_ADMIN=admin
INSTALAR_ADMIN_PASSWORD=$(secreto)
INSTALAR_DEMO=$demo
SESSION_SECRET_KEY=$(secreto)
EOF
      log "$ent/.env creado"
    else
      poner_var "$DIR/$ent/.env" IMAGEN "$imagen"
    fi
    if [[ ! -f "$DIR/$ent/app.env" ]]; then
      cat > "$DIR/$ent/app.env" <<EOF
# Configuracion de la app en el entorno $ent (ver app_fastapi/.env.example).
# Tras cambiarla: tareas.sh desplegar $ent <su version actual>
APP_NOMBRE=$nombre
APP_EMPRESA=
APP_LOGO=/static/img/logo.svg
AUTH_MODO=local
MODULO_MANTIS=0
MODULO_NEXUS=0
MODULO_ESCRITORIO=0
MODULO_CONECTORES=0
EOF
    fi
    touch "$DIR/$ent/historial.log"

    # usuario de la BD de este entorno, con permisos solo sobre sus bases
    local u p n t
    u=$(var "$DIR/$ent/.env" DB_USER); p=$(var "$DIR/$ent/.env" DB_PASSWORD)
    n=$(var "$DIR/$ent/.env" DB_NAME); t=$(var "$DIR/$ent/.env" DB_NAME_TEST)
    {
      echo "CREATE USER IF NOT EXISTS '$u'@'%' IDENTIFIED BY '$p';"
      echo "ALTER USER '$u'@'%' IDENTIFIED BY '$p';"
      echo "GRANT ALL PRIVILEGES ON \`$n\`.* TO '$u'@'%';"
      [[ -n "$t" ]] && echo "GRANT ALL PRIVILEGES ON \`$t\`.* TO '$u'@'%';"
      echo "FLUSH PRIVILEGES;"
    } | bd_sql
  done
  chmod 600 "$DIR"/*/.env

  preparar_clave_github
  chmod 700 "$DIR/tareas.sh" 2>/dev/null || true

  cat <<EOF

Listo. Faltan los pasos de GitHub y del proxy (docs/DESPLIEGUE.md).
Contraseña inicial del usuario admin de cada entorno (cámbiala al entrar):
  grep INSTALAR_ADMIN_PASSWORD $DIR/*/.env
EOF
}

preparar_clave_github() {
  local clave="$DIR/.ssh/github_despliegue"
  local auth="$HOME/.ssh/authorized_keys"
  mkdir -p "$DIR/.ssh" "$HOME/.ssh"
  chmod 700 "$DIR/.ssh" "$HOME/.ssh"
  if [[ ! -f "$clave" ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "github-despliegue-tareas" -f "$clave"
  fi
  # La clave de GitHub solo puede lanzar "tareas.sh ci" (nada de shell)
  local linea="command=\"$DIR/tareas.sh ci\",restrict $(cat "$clave.pub")"
  touch "$auth"; chmod 600 "$auth"
  grep -qF "$(cut -d' ' -f2 "$clave.pub")" "$auth" || echo "$linea" >> "$auth"

  local host_key
  host_key=$(cat /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | cut -d' ' -f1,2 || true)
  cat <<EOF

=== Para GitHub (Settings > Secrets and variables > Actions > New repository secret) ===
DEPLOY_SSH_KEY  = el contenido de:  cat $clave
DEPLOY_KNOWN_HOSTS = <dominio o IP del servidor> ${host_key:-(ssh-keyscan <servidor>)}
(La clave privada no sale de aqui salvo a ese secreto; no la pegues en chats.)
EOF
}

# ── desplegar ────────────────────────────────────────────────────────────────
TOKEN_USUARIO=""; TOKEN=""

traer_imagen() {   # traer_imagen IMAGEN:VERSION
  local ok=0
  if [[ -n "$TOKEN" ]]; then
    local cfg; cfg=$(mktemp -d)
    if printf '%s' "$TOKEN" | DOCKER_CONFIG=$cfg docker login ghcr.io -u "$TOKEN_USUARIO" --password-stdin >/dev/null 2>&1 \
       && DOCKER_CONFIG=$cfg docker pull -q "$1" >/dev/null 2>&1; then ok=1; fi
    rm -rf "$cfg"
  elif docker pull -q "$1" >/dev/null 2>&1; then
    ok=1
  fi
  if (( ! ok )); then
    docker image inspect "$1" >/dev/null 2>&1 \
      || die "no se puede descargar $1 (¿existe esa versión? si el paquete es privado: docker login ghcr.io)"
    log "uso la copia local de $1"
  fi
}

salud() {   # estado de salud del contenedor de un entorno
  local cid; cid=$(dc "$1" ps -q app 2>/dev/null || true)
  [[ -n "$cid" ]] || { echo "parado"; return; }
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "?"
}

esperar_salud() {
  local ent=$1 t=0 s
  while (( t < ESPERA_SALUD )); do
    s=$(salud "$ent")
    case $s in
      healthy) return 0;;
      unhealthy|exited|dead|parado) (( t > 20 )) && return 1;;
    esac
    sleep 5; t=$((t + 5))
  done
  return 1
}

cmd_desplegar() {
  local ent=${1:-} version=${2:-} origen=${3:-manual}
  [[ -n "$ent" && -n "$version" ]] || die "uso: tareas.sh desplegar ENT VERSION"
  entorno_valido "$ent"
  [[ "$version" == anterior ]] && { cmd_rollback "$ent"; return; }
  version_valida "$version"

  exec 9>"$DIR/$ent/.lock"
  flock -w 600 9 || die "otro despliegue en $ent no termina"

  local env="$DIR/$ent/.env" imagen anterior
  imagen=$(var "$env" IMAGEN)
  anterior=$(var "$env" VERSION)
  log "$ent: $imagen:$version (ahora: ${anterior:-nada})"
  traer_imagen "$imagen:$version"

  # el compose va dentro de la imagen: cada version trae el suyo
  local cid nuevo="$DIR/$ent/docker-compose.yml.nuevo"
  cid=$(docker create "$imagen:$version")
  if ! docker cp "$cid:/app/despliegue/servidor/docker-compose.yml" "$nuevo" 2>/dev/null; then
    [[ -f "$DIR/$ent/docker-compose.yml" ]] || { docker rm "$cid" >/dev/null; die "la imagen no trae despliegue/servidor/docker-compose.yml"; }
    cp "$DIR/$ent/docker-compose.yml" "$nuevo"
  fi
  # PRO trae tambien la version aprobada de este script (ver al final)
  local script_nuevo="$DIR/tareas.sh.nuevo"
  rm -f "$script_nuevo"
  [[ "$ent" == pro ]] && docker cp "$cid:/app/despliegue/servidor/tareas.sh" "$script_nuevo" 2>/dev/null || true
  docker rm "$cid" >/dev/null

  if [[ "$ent" != dev && -n "$anterior" ]]; then
    cmd_copia "$ent" "antes-de-$version"
  fi

  [[ -f "$DIR/$ent/docker-compose.yml" ]] && cp "$DIR/$ent/docker-compose.yml" "$DIR/$ent/docker-compose.yml.anterior"
  mv "$nuevo" "$DIR/$ent/docker-compose.yml"
  poner_var "$env" VERSION "$version"

  dc "$ent" up -d --remove-orphans
  log "esperando a que $ent responda..."
  if esperar_salud "$ent"; then
    printf '%s\t%s\tok\t%s\n' "$(ahora)" "$version" "$origen" >> "$DIR/$ent/historial.log"
    log "$ent ya está en $version"
    limpiar_imagenes "$imagen"
    actualizar_script "$script_nuevo"
    return 0
  fi

  printf '%s\t%s\tfallo\t%s\n' "$(ahora)" "$version" "$origen" >> "$DIR/$ent/historial.log"
  echo "---- últimas líneas del log de $ent ----" >&2
  dc "$ent" logs --tail 40 app >&2 || true
  if [[ -n "$anterior" && "$anterior" != "$version" ]]; then
    echo "La versión $version no arranca: vuelvo a $anterior." >&2
    poner_var "$env" VERSION "$anterior"
    [[ -f "$DIR/$ent/docker-compose.yml.anterior" ]] && cp "$DIR/$ent/docker-compose.yml.anterior" "$DIR/$ent/docker-compose.yml"
    dc "$ent" up -d --remove-orphans
    if esperar_salud "$ent"; then
      printf '%s\t%s\tok\trollback-automatico\n' "$(ahora)" "$anterior" >> "$DIR/$ent/historial.log"
      echo "$ent sigue en $anterior. Si $version cambió la base de datos, hay copia en copias/$ent/." >&2
    else
      echo "Tampoco arranca $anterior: revisa tareas.sh logs $ent" >&2
    fi
  fi
  exit 1
}

# Con cada version que llega a PRO, el servidor se queda con su tareas.sh
# (mv: el que esta corriendo sigue leyendo el suyo). Solo PRO: lo que esta
# en DEV aun no esta aprobado.
actualizar_script() {
  local nuevo=$1 propio="$DIR/tareas.sh"
  [[ -s "$nuevo" ]] || return 0
  if cmp -s "$nuevo" "$propio"; then rm -f "$nuevo"; return 0; fi
  if ! bash -n "$nuevo" 2>/dev/null; then
    log "el tareas.sh de esta versión no pasa bash -n: sigo con el de antes"
    rm -f "$nuevo"; return 0
  fi
  [[ -f "$propio" ]] && cp -p "$propio" "$propio.anterior"
  chmod 700 "$nuevo"
  mv -f "$nuevo" "$propio"
  log "tareas.sh actualizado con el de esta versión (el de antes: tareas.sh.anterior)"
}

# version buena anterior a la actual: la ultima desplegada con exito que no
# sea la actual ni una de la que ya se volvio atras
version_anterior() {
  local ent=$1 actual; actual=$(var "$DIR/$ent/.env" VERSION)
  awk -F'\t' -v actual="$actual" '
    $3=="ok" { orden[++n]=$2; estado[$2]="ok" }
    $3=="revertida" || $3=="fallo" { estado[$2]="mala" }
    END { for (i=n; i>0; i--) { v=orden[i]; if (v!=actual && estado[v]=="ok") { print v; exit } } }
  ' "$DIR/$ent/historial.log"
}

cmd_rollback() {
  local ent=${1:-} version=${2:-}
  [[ -n "$ent" ]] || die "uso: tareas.sh rollback ENT [VERSION]"
  entorno_valido "$ent"
  local actual; actual=$(var "$DIR/$ent/.env" VERSION)
  [[ "$version" == anterior ]] && version=""
  [[ -n "$version" ]] || version=$(version_anterior "$ent")
  [[ -n "$version" ]] || die "no hay una versión anterior buena en el historial de $ent"
  log "rollback de $ent: $actual -> $version"
  cmd_desplegar "$ent" "$version" rollback
  # se marca para que un segundo rollback no vuelva a ella
  if [[ -n "$actual" && "$actual" != "$version" ]]; then
    printf '%s\t%s\trevertida\t-> %s\n' "$(ahora)" "$actual" "$version" >> "$DIR/$ent/historial.log"
  fi
}

# borra las imagenes de versiones viejas ya desplegadas alguna vez (se quedan
# las de cada entorno y las ultimas VERSIONES_A_GUARDAR buenas, para poder
# volver a ellas sin descargar; las que nunca se desplegaron no se tocan)
limpiar_imagenes() {
  local imagen=$1 guardar="" usadas="" ent tag
  for ent in $ENTORNOS; do
    [[ -f "$DIR/$ent/.env" ]] || continue
    guardar+=" $(var "$DIR/$ent/.env" VERSION)"
    usadas+=" $(cut -f2 "$DIR/$ent/historial.log" 2>/dev/null | tr '\n' ' ' || true)"
    guardar+=" $(awk -F'\t' '$3=="ok"{print $2}' "$DIR/$ent/historial.log" 2>/dev/null | tac | awk '!v[$0]++' | head -n "$VERSIONES_A_GUARDAR" | tr '\n' ' ' || true)"
  done
  for tag in $(docker images "$imagen" --format '{{.Tag}}'); do
    [[ "$tag" == "<none>" ]] && continue
    case " $usadas " in *" $tag "*) ;; *) continue;; esac
    case " $guardar " in *" $tag "*) ;; *) docker rmi "$imagen:$tag" >/dev/null 2>&1 || true;; esac
  done
  docker image prune -f >/dev/null 2>&1 || true
}

# ── copias de la base de datos ───────────────────────────────────────────────
cmd_copia() {
  local ent=${1:-} etiqueta=${2:-manual}
  COPIA_HECHA=""
  [[ -n "$ent" ]] || die "uso: tareas.sh copia ENT"
  entorno_valido "$ent"
  local bd fichero
  bd=$(var "$DIR/$ent/.env" DB_NAME)
  mkdir -p "$DIR/copias/$ent"
  fichero="$DIR/copias/$ent/$(date +%Y%m%d-%H%M%S)_${etiqueta}.sql.gz"
  if ! bd_sql -N -e "SELECT 1 FROM information_schema.schemata WHERE schema_name='$bd'" | grep -q 1; then
    log "la base $bd aún no existe: sin copia"
    return 0
  fi
  (umask 077; docker exec -e MYSQL_PWD="$(bd_root)" tareas-bd \
     mariadb-dump -uroot --single-transaction --routines --triggers "$bd" | gzip > "$fichero")
  log "copia: $fichero"
  # solo las COPIAS_A_GUARDAR mas recientes
  ls -1t "$DIR/copias/$ent"/*.sql.gz 2>/dev/null | tail -n +$((COPIAS_A_GUARDAR + 1)) | xargs -r rm -f || true
  COPIA_HECHA=$fichero
}

cmd_restaurar() {
  local ent=${1:-} fichero=${2:-} si=${3:-}
  [[ -n "$ent" && -n "$fichero" ]] || die "uso: tareas.sh restaurar ENT FICHERO.sql.gz [--si]"
  entorno_valido "$ent"
  [[ -f "$fichero" ]] || fichero="$DIR/copias/$ent/$fichero"
  [[ -f "$fichero" ]] || die "no encuentro $fichero (mira: ls $DIR/copias/$ent)"
  local bd; bd=$(var "$DIR/$ent/.env" DB_NAME)
  if [[ "$si" != --si ]]; then
    read -r -p "Se BORRA la base $bd de $ent y se carga $(basename "$fichero"). ¿Seguir? (si/no) " r
    [[ "$r" == si ]] || die "cancelado"
  fi
  cmd_copia "$ent" "antes-de-restaurar"
  dc "$ent" stop app
  echo "DROP DATABASE IF EXISTS \`$bd\`; CREATE DATABASE \`$bd\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" | bd_sql
  gunzip -c "$fichero" | bd_sql "$bd"
  dc "$ent" start app
  esperar_salud "$ent" && log "$ent restaurado con $(basename "$fichero")" || die "$ent no arranca tras restaurar: tareas.sh logs $ent"
}

cmd_pro_a_dev() {
  entorno_valido pro; entorno_valido dev
  COPIA_HECHA=""
  cmd_copia pro "para-dev"
  [[ -n "$COPIA_HECHA" ]] || die "pro aún no tiene base de datos"
  cmd_restaurar dev "$COPIA_HECHA" "${1:-}"
}

# ── consulta ─────────────────────────────────────────────────────────────────
cmd_estado() {
  local ent
  printf '%-5s %-22s %s\n' ENT VERSION ESTADO
  for ent in $ENTORNOS; do
    [[ -f "$DIR/$ent/.env" ]] || { printf '%-5s %-22s %s\n' "$ent" "-" "sin preparar"; continue; }
    printf '%-5s %-22s %s\n' "$ent" "$(var "$DIR/$ent/.env" VERSION)" "$( [[ -f "$DIR/$ent/docker-compose.yml" ]] && salud "$ent" || echo 'sin desplegar')"
  done
}

cmd_historial() {
  entorno_valido "${1:-}"
  tail -n "${2:-20}" "$DIR/$1/historial.log"
}

cmd_logs() {
  entorno_valido "${1:-}"
  dc "$1" logs --tail 200 -f app
}

# ── entrada de GitHub Actions ────────────────────────────────────────────────
# La clave SSH de GitHub tiene command="tareas.sh ci": solo puede pedir estas
# ordenes. Por stdin llega "usuario token" para descargar de ghcr.io.
cmd_ci() {
  read -r -a orden <<< "${SSH_ORIGINAL_COMMAND:-}"
  if read -r -t 10 TOKEN_USUARIO TOKEN; then :; fi
  case "${orden[0]:-}" in
    desplegar) cmd_desplegar "${orden[1]:-}" "${orden[2]:-}" github ;;
    rollback)  cmd_rollback "${orden[1]:-}" "${orden[2]:-}" ;;
    estado)    cmd_estado ;;
    historial) cmd_historial "${orden[1]:-}" ;;
    *) die "orden no permitida: ${orden[0]:-(vacía)}" ;;
  esac
}

# ── importar: los datos de una instalacion de antes pasan a un entorno ──────
# ORIGEN es su contenedor de MariaDB (se vuelca en el momento; la base que no
# sea del sistema ni *_test, o la de BD_ORIGEN=...) o un volcado .sql/.sql.gz.
# Con APP_ORIGEN (el contenedor de esa app) se traen tambien los adjuntos.
# Antes se guarda copia de lo que hay (copias/<ENT>/).
cmd_importar() {   # importar ENT ORIGEN [APP_ORIGEN] [--si]
  local ent=${1:-} origen=${2:-} app_origen=${3:-} si=${4:-} fichero bd
  [[ "$app_origen" == --si ]] && { si=--si; app_origen=""; }
  [[ -n "$ent" && -n "$origen" ]] || die "uso: tareas.sh importar ENT CONTENEDOR_BD|VOLCADO.sql[.gz] [CONTENEDOR_APP] [--si]"
  entorno_valido "$ent"
  mkdir -p "$DIR/copias/$ent"
  fichero="$DIR/copias/$ent/$(date +%Y%m%d-%H%M%S)_importado.sql.gz"
  if [[ -f "$origen" ]]; then
    case "$origen" in
      *.sql.gz) cp "$origen" "$fichero" ;;
      *.sql)    gzip -c "$origen" > "$fichero" ;;
      *) die "el volcado tiene que ser .sql o .sql.gz" ;;
    esac
  else
    [[ "$origen" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] && docker inspect "$origen" >/dev/null 2>&1 \
      || die "'$origen' no es ni un fichero ni un contenedor (docker ps)"
    # la contraseña de root la lee el propio contenedor: no pasa por aqui
    local en='MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; export MYSQL_PWD; c=mariadb; command -v mariadb >/dev/null || c=mysql; d=mariadb-dump; command -v mariadb-dump >/dev/null || d=mysqldump'
    bd=${BD_ORIGEN:-}
    if [[ -z "$bd" ]]; then
      bd=$(docker exec "$origen" sh -c "$en; \$c -uroot -N -e 'SHOW DATABASES'" | tr -d '\r' \
           | grep -vxE 'information_schema|mysql|performance_schema|sys|.*_test' || true)
      [[ -n "$bd" ]] || die "no veo ninguna base de datos en $origen"
      [[ $(wc -l <<< "$bd") == 1 ]] || die "$origen tiene varias bases ($(echo $bd)): elige con BD_ORIGEN=<base> delante"
    fi
    [[ "$bd" =~ ^[A-Za-z0-9_]+$ ]] || die "base no válida: '$bd'"
    log "vuelco $bd de $origen"
    (umask 077; docker exec "$origen" sh -c "$en; exec \$d -uroot --single-transaction --routines --triggers $bd" | gzip > "$fichero") \
      || { rm -f "$fichero"; die "no se pudo volcar $bd de $origen"; }
  fi
  # sin pipefail: grep -q corta la tuberia en cuanto lo encuentra
  ( set +o pipefail; gunzip -c "$fichero" 2>/dev/null | grep -q -m1 'CREATE TABLE' ) || { rm -f "$fichero"; die "el volcado está vacío o no es de MariaDB"; }
  log "volcado: $fichero"
  cmd_restaurar "$ent" "$fichero" "$si"
  if [[ -n "$app_origen" ]]; then
    local cid; cid=$(dc "$ent" ps -q app)
    docker inspect "$app_origen" >/dev/null 2>&1 || die "no encuentro el contenedor $app_origen"
    log "copio los adjuntos de $app_origen"
    docker cp "$app_origen:/app/media" - | docker cp - "$cid:/app/" \
      || die "no se pudieron copiar los adjuntos (los datos sí están importados)"
  fi
  log "LISTO: $ent tiene los datos de $origen"
}

# ── keycloak: login unico (AUTH_MODO=sso) ────────────────────────────────────
cmd_keycloak() {
  local ent=${1:-} url=${2:-} issuer=${3:-} cliente=${4:-}
  [[ -n "$ent" && -n "$url" && -n "$issuer" ]] \
    || die "uso: tareas.sh keycloak ENT URL_DE_LA_APP ISSUER [CLIENTE]   (p. ej. keycloak dev https://dev.dominio.com/tareas https://dominio.com/auth/realms/REALM)"
  entorno_valido "$ent"
  cliente=${cliente:-tareas-$ent}
  [[ "$url" =~ ^https?://[^[:space:]|]+$ && "$issuer" =~ ^https?://[^[:space:]|]+$ ]] || die "URL o ISSUER no válidos"
  [[ "$cliente" =~ ^[A-Za-z0-9._-]+$ ]] || die "cliente no válido: '$cliente'"
  local secreto_kc
  read -rsp "Client secret de '$cliente' (Keycloak -> Clients -> $cliente -> Credentials): " secreto_kc; echo
  [[ "$secreto_kc" =~ ^[A-Za-z0-9._~-]+$ ]] || die "client secret vacío o con caracteres raros"
  local f="$DIR/$ent/app.env"
  poner_var "$f" AUTH_MODO sso
  poner_var "$f" KEYCLOAK_ISSUER "${issuer%/}"
  poner_var "$f" KEYCLOAK_CLIENT_ID "$cliente"
  poner_var "$f" KEYCLOAK_CLIENT_SECRET "$secreto_kc"
  poner_var "$f" APP_URL_PUBLICA "${url%/}"
  # quien entra: grupo /Tareas y sus subgrupos (/Tareas/Admin administra);
  # el grupo IT administra todas las apps
  poner_var "$f" KEYCLOAK_GRUPO_APP "${GRUPO_APP:-Tareas}"
  chmod 600 "$f"
  log "$ent: login con Keycloak ($cliente). Valid redirect URI del cliente: ${url%/}/login/sso/"
  local version; version=$(var "$DIR/$ent/.env" VERSION)
  if [[ -n "$version" ]]; then
    cmd_desplegar "$ent" "$version"
  else
    log "$ent aún no tiene versión: se aplicará en el primer despliegue"
  fi
}

uso() { sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  preparar)   shift; cmd_preparar "$@" ;;
  desplegar)  shift; cmd_desplegar "$@" ;;
  rollback)   shift; cmd_rollback "$@" ;;
  estado)     cmd_estado ;;
  historial)  shift; cmd_historial "$@" ;;
  copia)      shift; cmd_copia "$@" ;;
  restaurar)  shift; cmd_restaurar "$@" ;;
  pro-a-dev)  shift; cmd_pro_a_dev "$@" ;;
  logs)       shift; cmd_logs "$@" ;;
  keycloak)   shift; cmd_keycloak "$@" ;;
  importar)   shift; cmd_importar "$@" ;;
  ci)         cmd_ci ;;
  *)          uso; exit 1 ;;
esac
